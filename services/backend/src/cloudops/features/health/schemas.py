from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from cloudops.shared.dispatch_permit import DenyReason

HealthReason = Literal[
    "database_connected",
    "heartbeat_missing",
    "heartbeat_fresh",
    "heartbeat_stale",
    "invalid_evidence_time",
    "collector_unconfigured",
    "collector_configured",
    "collector_fixture",
    "collector_configuration_invalid",
    "scope_not_configured",
    "scope_target_missing",
    "scope_target_unavailable",
    "fixture_target",
    "reader_unconfigured",
    "scope_configuration_changed",
    "no_matching_observation",
    "observation_pending",
    "observation_expired",
    "observation_partial",
    "observation_denied",
    "observation_failed",
    "observation_stale",
    "observation_fresh",
    "model_feature_view",
    "executor_unconfigured",
    "executor_feature_view",
    "runner_unconfigured",
    "runner_not_observed",
    "report_invalid",
    "report_not_observed",
    "report_retained",
]


class HealthTarget(BaseModel):
    workload_id: UUID | None = None
    uid: str | None = Field(default=None, max_length=100)
    cluster: str = Field(max_length=100)
    namespace: str = Field(max_length=100)
    name: str = Field(max_length=100)


class HealthObservation(BaseModel):
    component: Literal[
        "database",
        "worker",
        "collector",
        "kubernetes",
        "diagnosis",
        "executor",
        "krr_runner",
        "krr_report",
    ]
    status: Literal[
        "available",
        "unavailable",
        "unconfigured",
        "configured",
        "never_observed",
        "fresh",
        "stale",
        "denied",
        "partial",
        "unknown",
        "historical",
        "see_feature",
    ]
    source: Literal[
        "runtime",
        "development_fixture",
        "collection_configuration",
        "live_kubernetes",
        "feature_view",
        "live_krr",
    ]
    assessed_at: datetime
    observed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    stale_after_seconds: int | None = None
    reason_code: HealthReason
    reason: str = Field(max_length=240)
    target: HealthTarget | None = None
    config_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    last_success_config_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    report_id: UUID | None = None
    configured_adapters: list[Literal["development_fixture", "live_kubernetes", "prometheus"]] = (
        Field(default_factory=list, max_length=2)
    )


class CollectionDispatch(BaseModel):
    status: Literal["allowed", "paused", "unknown"]
    reason_code: DenyReason | None = None


class HealthView(BaseModel):
    observations: list[HealthObservation] = Field(max_length=8)
    collection_dispatch: CollectionDispatch
