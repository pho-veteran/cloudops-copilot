from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cloudops.features.diagnosis.advice import RCAResult
from cloudops.features.diagnosis.profile import OutputLimitParam
from cloudops.features.diagnosis.schemas import CallKind


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisAttempt(BaseModel):
    number: int
    state: Literal["running", "expired", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class AnalysisView(BaseModel):
    schema_version: Literal["AnalysisView/v1"] = "AnalysisView/v1"
    status: str
    analysis_id: UUID | None = None
    phase: str | None = None
    reason: str | None = None
    identity: dict[str, str] | None = None
    context: dict | None = None
    result: RCAResult | None = None
    attempts: int = 0
    max_attempts: int = 3
    calls: int = 0
    max_calls: int = 3
    repairs: int = 0
    max_repairs: int = 1
    deadline: datetime | None = None
    wait_mode: str | None = None
    earliest_retry: datetime | None = None
    resume_allowed: bool = False
    resume_denial: str | None = None
    model_slot_busy: bool = False
    history: list[dict] = []
    attempt_history: list[AnalysisAttempt] = []


class ModelIdentityView(BaseModel):
    config_id: str | None = None
    config_version: str | None = None
    endpoint_label: str | None = None
    model: str | None = None
    behavior_digest: str | None = None
    policy_digest: str | None = None
    prompt_version: str | None = None
    context_version: str | None = None
    validator_version: str | None = None
    lifecycle_version: str | None = None


class TokenUsageView(BaseModel):
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class ProbeReceiptView(BaseModel):
    kind: CallKind | None
    observed_at: datetime | None
    http_status: int | None
    transport_success: bool | None
    schema_success: bool | None
    quality: Literal["not_established"] = "not_established"
    usage: TokenUsageView | None
    usage_status: Literal["observed", "unavailable", "invalid"]
    requested_model: str | None
    served_model: str | None
    response_sha256: str | None
    response_text_sha256: str | None
    finish_reason: str | None
    elapsed_ms: int | None
    response_bytes: int | None
    retry_after_seconds: int | None


class CheckView(BaseModel):
    id: UUID
    state: str
    created_at: datetime
    admitted_at: datetime | None
    deadline: datetime
    identity: ModelIdentityView
    receipt: ProbeReceiptView | None


class CheckPage(BaseModel):
    items: list[CheckView]
    next_offset: int | None
    assessed_at: datetime


class ProviderAttemptView(BaseModel):
    source: Literal["probe", "analysis", "evaluation"]
    admitted_at: datetime
    observed_at: datetime | None
    stale: bool
    transport: Literal["succeeded", "rejected", "unknown"]
    http_status: int | None
    outcome: CallKind | Literal["provider_outcome_unknown"] | None
    usage: TokenUsageView | None
    usage_status: Literal["observed", "unavailable", "invalid"]


class RCAValidationView(BaseModel):
    outcome: Literal["validated", "invalid"]
    purpose: Literal["operational", "evaluation"]
    observed_at: datetime
    stale: bool


class ModelLimitsView(BaseModel):
    request_deadline_seconds: float
    max_request_bytes: int
    max_message_bytes: int
    max_context_bytes: int
    output_limit_param: OutputLimitParam | None
    max_output_tokens: int | None
    max_analysis_calls: int
    max_probe_calls: int


class ModelStatus(BaseModel):
    availability: str
    identity: ModelIdentityView
    quality: Literal["qualified", "not_established"]
    qualification_cohort: str | None
    dispatch_allowed: bool
    dispatch_reason: str | None
    probe_dispatch_allowed: bool
    probe_dispatch_reason: str | None
    cooldown_seconds: int = 60
    assessed_at: datetime
    stale_after_seconds: int
    limits: ModelLimitsView | None
    last_check: CheckView | None = None
    last_attempt: ProviderAttemptView | None = None
    rca_validation: RCAValidationView | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
