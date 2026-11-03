from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from cloudops.features.investigations.schemas import (
    OriginView,
    RequestWindow,
    RunState,
    WindowSpec,
)
from cloudops.features.workloads.schemas import WorkloadView
from cloudops.shared.schemas import View


class ManualIncidentRequest(BaseModel):
    """Omitting both question and window keeps the committed three-field request contract."""

    workload_id: UUID
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    question: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"\S")
    window: WindowSpec | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Title must contain text.")
        return value


class AdmissionView(BaseModel):
    incident_id: UUID
    run_id: UUID
    state: RunState
    queue_reason: Literal["capacity"] | None
    window: RequestWindow | None


class SeverityView(BaseModel):
    value: str | None
    provenance: Literal["not_provided", "alert_label", "operator"]


class IncidentView(View):
    id: UUID
    workload_id: UUID
    title: str
    description: str
    state: Literal["open", "acknowledged", "resolved"]
    created_by: UUID | None
    created_at: datetime
    origin: OriginView
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    severity: SeverityView
    alert_signal: Literal["none", "firing", "resolved"]

    @model_validator(mode="before")
    @classmethod
    def from_row(cls, value):
        if isinstance(value, dict) or not hasattr(value, "origin_kind"):
            return value
        fields = (
            "id",
            "workload_id",
            "title",
            "description",
            "state",
            "created_by",
            "created_at",
            "first_seen_at",
            "last_seen_at",
            "alert_signal",
        )
        return {
            **{field: getattr(value, field) for field in fields},
            "origin": {"kind": value.origin_kind, "source_id": value.origin_source_id},
            "severity": {"value": value.severity, "provenance": value.severity_provenance},
        }


class IncidentDetail(IncidentView):
    workload: WorkloadView


class CreationAnchor(BaseModel):
    created_at: datetime
    id: UUID


class WorkQueueRun(BaseModel):
    id: UUID
    state: str
    origin_kind: Literal["operator", "alertmanager"]
    queue_reason: Literal["capacity"] | None
    reason: str
    requested_by: UUID | None
    requested_by_name: str | None
    created_at: datetime
    outcome_code: str | None


class WorkQueueExecution(BaseModel):
    id: UUID
    state: str
    classification: Literal[
        "active",
        "terminal_unknown_locked",
        "terminal_unknown_released",
        "terminal",
        "unrecognized",
    ]
    holds_lock: bool
    workload_id: UUID
    created_at: datetime


class WorkQueueItem(IncidentView):
    created_by_name: str | None
    acknowledged_by: UUID | None
    acknowledged_by_name: str | None
    acknowledged_at: datetime | None
    latest_run: WorkQueueRun | None
    run_blockers: list[Literal["queued", "waiting_for_quota", "failed", "insufficient_evidence"]]
    relevant_executions: list[WorkQueueExecution]


class IncidentPage(BaseModel):
    items: list[WorkQueueItem]
    next_offset: int | None
    creation_anchor: CreationAnchor
