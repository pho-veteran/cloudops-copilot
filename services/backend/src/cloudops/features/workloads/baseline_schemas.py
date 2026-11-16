"""Versioned selected-field baseline contracts, distinct from scale snapshots."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from cloudops.features.workloads.observation_schemas import (
    ConfigReference,
    Name,
    PublicObservation,
    Resources,
    SafeModel,
    Value,
)

PROFILE = "deployment-images-resources/v1"
LIMITATIONS = "Declared regular-container images and CPU/memory only. Replicas are observation context, not restored fields. Referenced contents, unsupported template fields, mutable image tags and external effects are unverified. This baseline grants no execution authority."
LEGACY_LIMITATIONS = "Legacy selected-field metadata without v1 two-observation verification. The original payload and digest are retained. Referenced contents and external effects are unverified. This baseline grants no execution authority."


class BaselineIdentity(SafeModel):
    cluster: Name
    namespace: Name
    name: Name
    uid: Name


class ContainerResources(SafeModel):
    requests: Resources
    limits: Resources


class RestorableContainer(SafeModel):
    name: Name
    image: Value
    resources: ContainerResources


class BaselineEnvelope(SafeModel):
    profile: Literal["deployment-images-resources/v1"] = PROFILE
    identity: BaselineIdentity
    containers: list[RestorableContainer] = Field(min_length=1, max_length=32)
    config_references: list[ConfigReference] = Field(max_length=128)


class Provenance(SafeModel):
    policy: Literal["two-fresh-healthy/v1"] = "two-fresh-healthy/v1"
    first: PublicObservation
    second: PublicObservation | None = None
    limitations: str = LIMITATIONS


class BaselinePayload(BaselineEnvelope):
    provenance: Provenance


class CandidateView(BaseModel):
    id: UUID
    workload_id: UUID
    digest: str
    snapshot: BaselinePayload
    created_at: datetime
    confirm_after: datetime
    expires_at: datetime
    server_time: datetime


class ConfirmBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class BaselineView(BaseModel):
    id: UUID
    workload_id: UUID
    candidate_id: UUID
    profile: Literal["deployment-images-resources/v1", "legacy/v0"]
    digest: str
    snapshot: dict
    confirmed_by: UUID
    confirmed_at: datetime
    age_seconds: float
    limitations: str = LIMITATIONS


class BaselinePage(BaseModel):
    items: list[BaselineView]
    next_cursor: str | None = None


class RestoreContext(BaseModel):
    state: Literal["baseline_context", "manual_only"]
    reason: Literal[
        "missing",
        "legacy",
        "mismatch",
        "unavailable",
        "unsupported",
        "config_unverified",
        "no_supported_diff",
        "restore_not_implemented",
        "exact_plan_required",
    ]
    execution_eligible: Literal[False] = False
    baseline: BaselineView | None = None
    current_uid: str | None = None
    supported_diff: list[dict] | None = None
    limitations: str = LIMITATIONS
