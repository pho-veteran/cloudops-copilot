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
    "analysis_insufficient",
    "analysis_completed",
    "analysis_failed",
    "operator_cancelled",
    "incident_resolved",
    "scope_retired",
    "execution_blocking",
    "analysis_unavailable",
    "collection_failed",
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


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["available", "partial", "unavailable", "not_applicable"]
    reason_codes: list[str] = Field(max_length=16)


class SourceTimes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime | None
    collected_at: AwareDatetime
    time_relation: Literal[
        "current_observation", "within_requested_window", "extends_after_window", "not_captured"
    ]


class SourceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_key: str
    payload_kind: Literal["state", "events", "logs", "series", "fixture"]
    payload_version: int
    coverage: Coverage
    source_times: SourceTimes
    query_metadata: dict | None = None
    redacted_bounded_payload: dict | None = None


class PackCoverage(BaseModel):
    pack: str
    status: Literal["available", "partial", "unavailable"]
    missing_required: list[str]
    optional_gaps: list[str]


class EvidenceView(View):
    id: UUID
    run_id: UUID
    workload_uid: str
    source_key: str
    source_kind: Literal["state", "events", "logs", "series", "fixture"]
    catalog_version: str
    schema_version: Literal[1, 2]
    payload_kind: Literal["state", "events", "logs", "series", "fixture"]
    payload_version: int
    observed_at: datetime | None
    collected_at: datetime
    requested_window: RequestWindow | None
    time_relation: Literal[
        "current_observation", "within_requested_window", "extends_after_window", "not_captured"
    ]
    coverage: Coverage
    query_metadata: dict | None
    sha256: str | None
    redaction_policy_version: str
    payload_ref: UUID | None
    expires_at: datetime | None
    pinned: bool
    content_status: Literal["available", "unavailable", "expired"]


class EvidenceContent(BaseModel):
    evidence_id: UUID
    run_id: UUID
    schema_version: Literal[1, 2]
    payload_kind: Literal["state", "events", "logs", "series", "fixture"]
    payload_version: int
    sha256: str
    payload: dict


class AttemptView(View):
    number: int
    state: Literal["running", "expired", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    # Source checkpoints committed by this attempt; null for non-checkpointed history.
    sources_committed: int | None


class RunProgress(BaseModel):
    sources_total: int
    sources_committed: int
    sources_reused: int
    finalized: bool
    attempt: int | None
    max_attempts: int


DispatchReason = Literal[
    "recovery_mode", "class_denied", "permit_missing", "permit_stale", "permit_invalid"
]


class DispatchStatus(BaseModel):
    status: Literal["allowed", "paused"]
    reason: DispatchReason | None


CancelReason = Literal[
    "queued",
    "waiting_without_provider_call",
    "collection_started",
    "analysis_started",
    "provider_call_in_flight",
    "finished",
    "already_cancelled",
]


class CancelState(BaseModel):
    allowed: bool
    reason: CancelReason


class AnalysisProjection(BaseModel):
    status: str
    analysis_id: UUID | None = None
    reason: str | None = None
    calls: int = 0
    attempts: int = 0


class RunView(View):
    analysis: AnalysisProjection | None = None
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
    pack_coverage: list[PackCoverage] | None
    evidence: list[EvidenceView]
    attempts: list[AttemptView]
    progress: RunProgress | None
    dispatch: DispatchStatus | None
    cancel: CancelState
    diagnosis: Literal["unavailable"] = "unavailable"
    recovery: Literal["unavailable"] = "unavailable"


class RunPage(BaseModel):
    items: list[RunView]
    next_offset: int | None
