from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from cloudops.shared.schemas import View

RunState = Literal[
    "queued",
    "collecting",
    "analyzing",
    "waiting_for_quota",
    "insufficient_evidence",
    "completed",
    "failed",
    "cancelled",
]
QuestionSource = Literal["operator", "title_default", "automatic", "not_captured"]
WindowSource = Literal[
    "preset_15",
    "preset_30",
    "preset_60",
    "explicit",
    "legacy_default",
    "automatic_default",
]
OutcomeCode = Literal[
    "operator_cancelled", "incident_resolved", "scope_retired", "execution_blocking"
]


class WindowPreset(BaseModel):
    """A relative window, resolved to absolute UTC once at first admission."""

    model_config = ConfigDict(extra="forbid")
    preset: Literal[15, 30, 60] = 30


class WindowInterval(BaseModel):
    """An explicit interval: 1 to 60 minutes and not ending in the future."""

    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime
    end: AwareDatetime


WindowSpec = WindowPreset | WindowInterval


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_id: UUID
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    window: WindowSpec = Field(default_factory=WindowPreset)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"\S")


class OriginView(BaseModel):
    kind: Literal["operator", "alertmanager"]
    source_id: str | None


class RequestWindow(BaseModel):
    start: datetime
    end: datetime
    source: WindowSource


class FixturePayload(BaseModel):
    label: Literal["Synthetic development evidence; no live cluster was contacted."]
    workload: str
    observation: Literal["Live workload health and telemetry are unavailable."]


class EvidenceView(View):
    id: UUID
    run_id: UUID
    workload_uid: str
    source: Literal["development_fixture"]
    schema_version: Literal[1]
    observed_at: datetime
    collected_at: datetime
    sha256: str
    payload: FixturePayload


class AttemptView(View):
    number: int
    state: Literal["running", "expired", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class RunView(View):
    id: UUID
    incident_id: UUID
    state: RunState
    reason: str
    created_at: datetime
    finished_at: datetime | None
    requested_by: UUID | None
    origin: OriginView
    question: str | None
    question_source: QuestionSource
    window: RequestWindow | None
    queue_reason: Literal["capacity"] | None
    outcome_code: OutcomeCode | None
    evidence: list[EvidenceView]
    attempts: list[AttemptView]
    diagnosis: Literal["unavailable"] = "unavailable"
    recovery: Literal["unavailable"] = "unavailable"


class RunPage(BaseModel):
    items: list[RunView]
    next_offset: int | None
