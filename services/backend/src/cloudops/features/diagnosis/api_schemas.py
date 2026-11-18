from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cloudops.features.diagnosis.advice import RCAResult


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


class CheckView(BaseModel):
    id: UUID
    state: str
    created_at: datetime
    deadline: datetime
    identity: dict[str, str]
    receipt: dict | None


class CheckPage(BaseModel):
    items: list[CheckView]
    next_offset: int | None


class ModelUsage(BaseModel):
    source: Literal["check", "analysis"]
    observed_at: datetime
    status: Literal["observed", "unavailable", "invalid"]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelCallEvidence(BaseModel):
    source: Literal["check", "analysis"]
    observed_at: datetime
    kind: str | None
    http_status: int | None


class ModelRcaEvidence(BaseModel):
    status: Literal["validated_advice", "validated_abstention"]
    observed_at: datetime
    analysis_id: UUID


class ModelConfiguration(BaseModel):
    mode: str | None = None
    api_root: str | None = None
    credential_reference: str | None = None
    timeout_seconds: float | None = None
    max_input_bytes: int | None = None
    max_response_bytes: int | None = None
    output_limit_parameter: str | None = None
    output_limit: int | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    policy_digest: str | None = None
    max_request_bytes: int | None = None
    max_target_bytes: int | None = None


class ModelStatus(BaseModel):
    availability: str
    identity: dict[str, str]
    quality: Literal["qualified", "not_established"]
    qualification_cohort: str | None
    dispatch_allowed: bool
    dispatch_reason: str | None
    cooldown_seconds: int = 60
    last_check: CheckView | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    configuration: ModelConfiguration
    latest_call: ModelCallEvidence | None = None
    latest_usage: ModelUsage | None = None
    validated_rca: ModelRcaEvidence | None = None
