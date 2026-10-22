from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from cloudops.features.workloads.schemas import WorkloadView
from cloudops.shared.schemas import View


class ManualIncidentRequest(BaseModel):
    workload_id: UUID
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Title must contain text.")
        return value


class AdmissionView(BaseModel):
    incident_id: UUID
    run_id: UUID


class IncidentView(View):
    id: UUID
    workload_id: UUID
    title: str
    description: str
    state: Literal["open"]
    created_by: UUID
    created_at: datetime


class IncidentDetail(IncidentView):
    workload: WorkloadView


class IncidentPage(BaseModel):
    items: list[IncidentView]
    next_offset: int | None
