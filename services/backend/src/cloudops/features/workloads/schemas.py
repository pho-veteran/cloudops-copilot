from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from cloudops.shared.schemas import View


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
    health: Literal["unavailable"] = "unavailable"


class WorkloadPage(BaseModel):
    items: list[WorkloadView]
    next_offset: int | None


ObservationStatus = Literal["healthy", "progressing", "degraded", "scaled_to_zero", "unknown"]


class ObservedContainer(BaseModel):
    name: str
    init: bool
    ready: bool
    restart_count: int = Field(ge=0)
    waiting_reason: str | None = None


class ObservedPod(BaseModel):
    uid: str
    name: str
    phase: str | None
    ready: bool
    containers: list[ObservedContainer]


class ObservationCoverage(BaseModel):
    complete: bool
    missing_sources: list[str]


class ConfigReference(BaseModel):
    kind: Literal["config_map", "secret", "service_account"]
    name: str
    source: str


class WorkloadObservationPayload(BaseModel):
    """Sanitized direct observation; never carries provider text or environment literals."""

    deployment_uid: str
    generation: int
    resource_version: str
    observed_generation: int | None
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    updated_replicas: int
    total_replicas: int
    status: ObservationStatus
    reasons: list[str]
    coverage: ObservationCoverage
    pods: list[ObservedPod]
    restart_total: int = Field(ge=0)
    restart_delta: int | None = Field(default=None, ge=0)
    ready_containers: int = Field(ge=0)
    total_containers: int = Field(ge=0)
    template_fields: dict
    config_references: list[ConfigReference]
    application_telemetry: Literal["unverified"] = "unverified"
