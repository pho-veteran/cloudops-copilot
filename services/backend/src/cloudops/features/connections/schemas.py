from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cloudops.shared.schemas import View

Component = Literal["cluster", "prometheus", "database", "worker"]
Source = Literal["live_kubernetes", "prometheus", "runtime"]
Outcome = Literal["available", "unavailable", "partial", "stale"]
Status = Literal[
    "available",
    "unavailable",
    "partial",
    "stale",
    "unconfigured",
    "never_observed",
    "missing",
    "denied",
    "pending",
]
Reason = Literal[
    "database_read",
    "heartbeat_fresh",
    "heartbeat_missing",
    "heartbeat_stale",
    "deployment_read",
    "query_succeeded",
    "metrics_missing",
    "metrics_partial",
    "metrics_stale",
    "scope_missing",
    "scope_changed",
    "target_changed",
    "config_disabled",
    "config_missing",
    "config_invalid",
    "config_changed",
    "permission_denied",
    "redirect_refused",
    "timeout",
    "transport_unavailable",
    "invalid_response",
    "response_limit",
    "check_pending",
    "never_observed",
    "binding_changed",
    "evidence_stale",
    "invalid_timestamp",
]
CoverageReason = Literal[
    "no_samples", "ownership_unproven", "truncated", "stale", "invalid_samples"
]


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Component


class CheckTarget(BaseModel):
    workload_id: UUID
    uid: str
    cluster: str
    namespace: str
    name: str


class MetricsCheck(BaseModel):
    query: Literal["memory_working_set"] = "memory_working_set"
    query_succeeded_at: datetime
    coverage: Literal["available", "partial", "missing"]
    coverage_reasons: list[CoverageReason]
    latest_evaluation_at: datetime | None
    raw_freshness: Literal["unknown"] = "unknown"


class ReceiptView(View):
    id: UUID
    component: Component
    status: Outcome
    reason: str
    source: Source
    created_by: UUID
    observed_at: datetime
    reason_code: Reason | None = None
    attempted_at: datetime | None = None
    finished_at: datetime | None = None
    evidence_at: datetime | None = None
    target: CheckTarget | None = None
    config_id: str | None = None
    metrics: MetricsCheck | None = None
    historical: bool = True


class CheckStatus(BaseModel):
    component: Component
    source: Source
    status: Status
    reason_code: Reason
    reason: str
    assessed_at: datetime
    target: CheckTarget | None
    config_id: str | None
    observed_at: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    stale_after_seconds: int | None
    latest_check_id: UUID | None
    metrics: MetricsCheck | None
