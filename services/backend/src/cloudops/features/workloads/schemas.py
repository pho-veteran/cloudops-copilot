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
