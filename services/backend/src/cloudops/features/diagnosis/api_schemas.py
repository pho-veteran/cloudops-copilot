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


class ModelStatus(BaseModel):
    availability: str
    identity: dict[str, str]
    quality: Literal["qualified", "not_established"]
    qualification_cohort: str | None
    dispatch_allowed: bool
    dispatch_reason: str | None
    cooldown_seconds: int = 60
    last_check: CheckView | None = None
