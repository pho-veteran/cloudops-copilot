import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.incidents.schemas import IncidentDetail
from cloudops.features.incidents.service import get_incident
from cloudops.features.investigations.service import list_runs
from cloudops.features.recovery.api import ExecutionView
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.service import invalidate
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api/incidents", tags=["incident lifecycle"], responses=ERROR_RESPONSES)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["acknowledge", "resolve", "reopen"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class ActivityView(View):
    id: UUID
    incident_id: UUID
    kind: str
    text: str
    actor_id: UUID
    created_at: datetime
    snapshot: dict | None


def handoff_data(db, incident_id):
    incident = get_incident(db, incident_id)
    activities = db.scalars(
        select(IncidentActivity)
        .where(IncidentActivity.incident_id == incident_id)
        .order_by(IncidentActivity.created_at, IncidentActivity.id)
        .limit(100)
    ).all()
    executions = db.scalars(
        select(Execution)
        .join(ActionPlan)
        .where(ActionPlan.incident_id == incident_id)
        .order_by(Execution.created_at)
        .limit(25)
    ).all()
    return {
        "incident": incident.model_dump(mode="json"),
        "investigations": list_runs(db, incident_id, 25, 0).model_dump(mode="json"),
        "activity": [
            {
                "id": str(a.id),
                "kind": a.kind,
                "text": a.text,
                "actor_id": str(a.actor_id),
                "created_at": a.created_at.isoformat(),
            }
            for a in activities
        ],
        "executions": [ExecutionView.model_validate(e).model_dump(mode="json") for e in executions],
        "limitations": "Stored observations and human notes only. No new diagnosis was generated. Historical unknown execution outcomes and target locks remain independent of incident resolution.",
    }


@router.post(
    "/{incident_id}/notes",
    operation_id="add_incident_note",
    response_model=ActivityView,
    status_code=201,
)
def note(
    incident_id: UUID,
    body: NoteBody,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    with db.begin():
        if not db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update()):
            raise DomainError(404, "incident_not_found", "Incident not found.")
        row = IncidentActivity(
            incident_id=incident_id, kind="note", text=body.text, actor_id=actor.id
        )
        db.add(row)
        db.flush()
        return ActivityView.model_validate(row)


@router.post(
    "/{incident_id}/lifecycle", operation_id="transition_incident", response_model=IncidentDetail
)
def transition(
    incident_id: UUID,
    body: TransitionBody,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    with db.begin():
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if not incident:
            raise DomainError(404, "incident_not_found", "Incident not found.")
        expected = {"acknowledge": ("open", "acknowledged"), "reopen": ("resolved", "open")}
        if body.action == "resolve":
            if incident.state == "resolved":
                raise DomainError(
                    409, "incident_state_changed", "This incident is already resolved."
                )
            active = db.scalar(
                select(Execution.id)
                .join(ActionPlan)
                .where(
                    ActionPlan.incident_id == incident_id,
                    Execution.state.in_(["queued", "executing"]),
                )
            )
            if active:
                raise DomainError(
                    409,
                    "execution_active",
                    "An admitted execution cannot be hidden by resolution. Wait for its recorded outcome.",
                )
            invalidate(db, incident_id)
            incident.state = "resolved"
        else:
            before, after = expected[body.action]
            if incident.state != before:
                raise DomainError(
                    409,
                    "incident_state_changed",
                    "Refresh the incident before making this transition.",
                )
            incident.state = after
        row = IncidentActivity(
            incident_id=incident_id, kind=body.action, text=body.reason, actor_id=actor.id
        )
        db.add(row)
        db.flush()
        if body.action == "resolve":
            row.snapshot = handoff_data(db, incident_id)
        return get_incident(db, incident_id)


@router.get(
    "/{incident_id}/activity",
    operation_id="list_incident_activity",
    response_model=list[ActivityView],
    dependencies=[Depends(current_session)],
)
def activity(incident_id: UUID, db: Session = Depends(get_db)):
    get_incident(db, incident_id)
    return db.scalars(
        select(IncidentActivity)
        .where(IncidentActivity.incident_id == incident_id)
        .order_by(IncidentActivity.created_at.desc(), IncidentActivity.id.desc())
        .limit(100)
    ).all()


@router.get(
    "/{incident_id}/handoff",
    operation_id="export_incident_handoff",
    response_class=PlainTextResponse,
    dependencies=[Depends(current_session)],
)
def handoff(
    incident_id: UUID,
    resolution_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if resolution_id:
        row = db.get(IncidentActivity, resolution_id)
        if not row or row.incident_id != incident_id or row.kind != "resolve" or not row.snapshot:
            raise DomainError(
                404, "resolution_not_found", "Saved resolution not found for this incident."
            )
        data = row.snapshot
    else:
        data = handoff_data(db, incident_id)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", content)), default=0))
    return PlainTextResponse(
        f"# Incident handoff\n\n{fence}json\n{content}\n{fence}\n",
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.md"'},
    )
