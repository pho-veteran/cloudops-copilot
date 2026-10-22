from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HealthObservation(BaseModel):
    component: Literal["database", "worker", "collector", "diagnosis", "executor"]
    status: Literal["available", "unavailable", "stale"]
    observed_at: datetime
    last_success_at: datetime | None
    reason: str
    source: Literal["runtime", "development_fixture", "unimplemented"]


class HealthView(BaseModel):
    observations: list[HealthObservation]
