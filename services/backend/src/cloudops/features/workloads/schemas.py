from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from cloudops.shared.schemas import View
from cloudops.features.workloads.observation_schemas import Health, ObservationSummary


class DevelopmentWorkload(BaseModel):
    cluster: str = Field(min_length=1, max_length=100)
    namespace: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    uid: str = Field(min_length=1, max_length=100)
    source: Literal["development_fixture", "live_kubernetes"] = "development_fixture"


class WorkloadView(View):
    id: UUID
    cluster: str
    namespace: str
    name: str
    uid: str
    active: bool
    source: Literal["development_fixture", "live_kubernetes"] = "development_fixture"
    health: Health = "unknown"
    observation: ObservationSummary | None = None
    retirement_reason: Literal["removed", "deleted", "replaced"] | None = None


class WorkloadPage(BaseModel):
    items: list[WorkloadView]
    next_offset: int | None


class ScopeTargetView(View):
    id: UUID
    cluster: str
    namespace: str
    name: str
    configured: bool
    workload_id: UUID | None
    uid: str | None = None
    state: Literal["current", "missing", "unavailable", "removed"]
    reason: (
        Literal["not_found", "permission_denied", "cluster_unavailable", "budget_exhausted"] | None
    )
    last_attempt_at: datetime
    last_success_at: datetime | None


class ScopePage(BaseModel):
    items: list[ScopeTargetView]
    next_offset: int | None
