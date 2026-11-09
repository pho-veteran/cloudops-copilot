from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cloudops.features.investigations.schemas import RequestWindow

MarkerKind = Literal[
    "window_start",
    "window_end",
    "rollout_observation",
    "incident_activity",
    "execution_created",
    "execution_finished",
]


class TimelineMarker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: MarkerKind
    at: datetime
    id: UUID
    label: str = Field(max_length=240)
    detail: dict = Field(default_factory=dict)


class RunTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: Literal["RunTimeline/v1"] = Field(default="RunTimeline/v1", alias="schema")
    run_id: UUID
    window: RequestWindow | None
    markers: list[TimelineMarker] = Field(default_factory=list, max_length=500)
    truncated: bool
    limitations: str = Field(max_length=240)
