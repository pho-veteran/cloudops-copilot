"""Whitelisted observation contracts, shared by the observer and direct consumers."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Health = Literal["healthy", "progressing", "degraded", "scaled_to_zero", "unknown"]
Coverage = Literal["complete", "partial", "unknown"]
Reason = Literal[
    "not_observed",
    "fixture",
    "inactive",
    "cluster_unavailable",
    "permission_denied",
    "target_changed",
    "cluster_identity_mismatch",
    "coverage_changed",
    "budget_exhausted",
    "status_missing",
    "ownership_invalid",
    "current_replicaset_ambiguous",
    "read_window_changed",
    "attempt_timeout",
    "lease_expired",
    "superseded",
    "payload_limit",
    "invalid_response",
    "pagination_invalid",
    "history_expired",
    "progress_deadline_exceeded",
    "crash_loop",
    "image_pull",
    "container_terminated",
    "unschedulable",
    "rollout_pending",
    "unsupported_reference",
    "stale",
]
Name = Annotated[str, Field(min_length=1, max_length=253)]
Value = Annotated[str, Field(min_length=1, max_length=512)]
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(SafeModel):
    workload_id: UUID
    cluster: Name
    namespace: Name
    name: Name
    uid: Name
    epoch: int


class Resources(SafeModel):
    cpu: Value | None = Field(default=None, exclude_if=lambda value: value is None)
    memory: Value | None = Field(default=None, exclude_if=lambda value: value is None)


class TemplateContainer(SafeModel):
    name: Name
    image: Value
    requests: Resources
    limits: Resources


class ConfigReference(SafeModel):
    path: Value
    kind: Literal["Secret", "ConfigMap", "Field", "Resource"]
    name: Name | None = None
    key: Value | None = None
    optional: bool | None = None
    selector: Value | None = None
    container_name: Name | None = None
    divisor: Value | None = None
    api_version: Value | None = None


class ContainerObservation(SafeModel):
    name: Name
    kind: Literal["regular", "init"]
    ready: bool | None
    started: bool | None
    state: Literal["running", "waiting", "terminated", "unknown"]
    restart_count: Counter | None
    exit_code: int | None = None
    reason: Reason | None = None


class PodObservation(SafeModel):
    uid: Name
    name: Name
    replicaset_uid: Name
    current: bool
    ready: bool | None
    phase: Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"]
    terminating: bool
    excluded: bool
    containers: list[ContainerObservation] = Field(max_length=32)


class DeploymentObservation(SafeModel):
    generation: Counter
    resource_version: Name
    end_resource_version: Name
    observed_generation: Counter | None
    desired: Counter
    ready: Counter | None
    available: Counter | None
    updated: Counter | None
    replicas: Counter | None


class Observation(SafeModel):
    schema_version: Literal[1] = 1
    observation_id: UUID
    identity: Identity
    started_at: datetime
    observed_at: datetime
    deployment: DeploymentObservation
    replicaset_resource_version: Name
    pod_resource_version: Name
    current_replicaset_uid: Name | None
    coverage: Coverage
    health: Health
    reasons: list[Reason] = Field(max_length=16)
    pods: list[PodObservation] = Field(max_length=100)
    unrelated_pods: Counter
    template_fields: list[TemplateContainer] = Field(max_length=32)
    pod_template_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    config_references: list[ConfigReference] = Field(max_length=128)
    limitations: Literal[
        "Kubernetes rollout only; application traffic, referenced contents and external effects are unverified."
    ] = "Kubernetes rollout only; application traffic, referenced contents and external effects are unverified."


class Comparison(SafeModel):
    state: Literal["comparable", "not_comparable", "coverage_changed"] = "not_comparable"
    from_observation_id: UUID | None = None
    to_observation_id: UUID | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    from_generation: int | None = None
    to_generation: int | None = None
    restart_delta: Counter | None = None
    changes: list[
        Literal[
            "selected_fields_changed",
            "references_changed",
            "generation_changed",
            "replicas_changed",
        ]
    ] = Field(default_factory=list, max_length=4)


class ObservationSummary(SafeModel):
    evaluated_at: datetime
    effective_health: Health = "unknown"
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    coverage: Coverage = "unknown"
    age_seconds: float | None = None
    stale_after_seconds: Literal[60] = 60
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    latest_outcome: (
        Literal["running", "complete", "partial", "failed", "expired", "superseded"] | None
    ) = None
    refreshing: bool = False
    refresh_remaining_seconds: float | None = None
    poll_lag_seconds: float | None = None
    reasons: list[Reason] = Field(default_factory=list, max_length=16)
    last_failure_reason: Reason | None = None
    desired: int | None = None
    ready: int | None = None


class PublicObservation(SafeModel):
    """Public evidence deliberately excludes private template/reference extraction."""

    observation_id: UUID
    identity: Identity
    started_at: datetime
    observed_at: datetime
    deployment: DeploymentObservation
    current_replicaset_uid: Name | None
    coverage: Coverage
    health: Health
    reasons: list[Reason]
    pods: list[PodObservation]
    unrelated_pods: Counter
    limitations: str


class ObservationDetail(SafeModel):
    summary: ObservationSummary
    current: PublicObservation | None = None
    historical: PublicObservation | None = None
    comparison: Comparison


class HistoryItem(SafeModel):
    observation_id: UUID
    observed_at: datetime
    generation: int
    health: Health
    coverage: Coverage


class ObservationPage(SafeModel):
    items: list[HistoryItem]
    next_cursor: str | None


class DirectOutcome(SafeModel):
    outcome: Literal["complete", "partial", "failed", "busy", "superseded"]
    reason: Reason | None = None
    observation: Observation | None = None
