from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from cloudops.shared.schemas import View


class FixturePayload(BaseModel):
    label: Literal["Synthetic development evidence; no live cluster was contacted."]
    workload: str
    observation: Literal["Live workload health and telemetry are unavailable."]


class EvidenceView(View):
    id: UUID
    run_id: UUID
    workload_uid: str
    source: Literal["development_fixture"]
    schema_version: Literal[1]
    observed_at: datetime
    collected_at: datetime
    sha256: str
    payload: FixturePayload


class AttemptView(View):
    number: int
    state: Literal["running", "expired", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class RunView(View):
    id: UUID
    incident_id: UUID
    state: Literal["queued", "collecting", "insufficient_evidence", "failed"]
    reason: str
    created_at: datetime
    finished_at: datetime | None
    requested_by: UUID
    evidence: list[EvidenceView]
    attempts: list[AttemptView]
    diagnosis: Literal["unavailable"] = "unavailable"
    recovery: Literal["unavailable"] = "unavailable"


class RunPage(BaseModel):
    items: list[RunView]
    next_offset: int | None
