"""Alertmanager webhook v4 envelope and the sanitized intake receipt transport."""

import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloudops.features.incidents.intake_settings import MAX_ALERTS

RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})"
)


def canonical_start(value: str) -> tuple[str, datetime]:
    """UTC key with all nine fractional digits, plus a microsecond value for display.

    Equivalent spellings (offsets, trailing zeros) share a key; nanosecond differences do not.
    """
    match = RFC3339.fullmatch(value)
    if not match:
        raise ValueError("startsAt must be an RFC3339 timestamp.")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    moment = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc
    )
    if zone not in ("Z", "z"):
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("startsAt has an invalid offset.")
        offset = timedelta(hours=hours, minutes=minutes)
        moment = moment - offset if zone[0] == "+" else moment + offset
    if moment.year < 1970:
        raise ValueError("startsAt must be a real episode start, not the zero time.")
    nanos = (fraction or "").ljust(9, "0")
    key = f"{moment:%Y-%m-%dT%H:%M:%S}.{nanos}Z"
    return key, moment + timedelta(microseconds=int(nanos[:6]))


def bounded_pairs(value: dict[str, str], key_limit: int, value_limit: int) -> dict[str, str]:
    if len(value) > 64:
        raise ValueError("At most 64 labels or annotations are accepted.")
    if any(len(k) > key_limit or len(v) > value_limit for k, v in value.items()):
        raise ValueError("A label or annotation exceeds its size bound.")
    return value


class AlertEntry(BaseModel):
    # Unknown sender fields are ignored for forward compatibility; none select an origin.
    model_config = ConfigDict(extra="ignore")
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = Field(max_length=64)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{1,64}$")

    @field_validator("labels")
    @classmethod
    def label_bounds(cls, value):
        return bounded_pairs(value, 128, 1024)

    @field_validator("annotations")
    @classmethod
    def annotation_bounds(cls, value):
        # Size-checked only; annotations are never stored or echoed.
        return bounded_pairs(value, 128, 4096)

    @field_validator("startsAt")
    @classmethod
    def start(cls, value):
        canonical_start(value)
        return value

    @property
    def start_key(self) -> tuple[str, datetime]:
        return canonical_start(self.startsAt)


class AlertmanagerEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: Literal["4"]
    status: Literal["firing", "resolved"]
    truncatedAlerts: int = Field(default=0, ge=0)
    alerts: list[AlertEntry] = Field(min_length=1, max_length=MAX_ALERTS)


Outcome = Literal[
    "accepted", "deferred", "duplicate", "resolved_signal", "ignored", "rejected", "retryable"
]


class IntakeResult(BaseModel):
    delivery_id: UUID
    counts: dict[Outcome, int]


class AlertReceiptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    delivery_id: UUID
    received_at: datetime
    source_id: str
    alert_status: Literal["firing", "resolved"]
    fingerprint: str
    alertname: str | None
    namespace: str | None
    target_kind: Literal["deployment", "pod"] | None
    target_name: str | None
    outcome: Outcome
    reason: str | None
    incident_id: UUID | None
    run_id: UUID | None
    workload_id: UUID | None


class AlertReceiptPage(BaseModel):
    configured: bool
    source_id: str | None
    items: list[AlertReceiptView]
    next_cursor: str | None
