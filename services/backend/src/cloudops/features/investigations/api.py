from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents.schemas import AdmissionView
from cloudops.features.incidents.service import admit, cancel_run, get_incident
from cloudops.features.investigations.schemas import CancelRequest, RunPage, RunRequest, RunView
from cloudops.features.investigations.service import get_run, list_runs
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(
    prefix="/api/incidents/{incident_id}/runs", tags=["investigations"], responses=ERROR_RESPONSES
)


@router.post("", operation_id="create_run", response_model=AdmissionView, status_code=201)
def create_run(
    incident_id: UUID,
    idempotency_key: UUID = Header(),
    body: RunRequest | None = Body(
        default=None,
        description="Required for a new request. An empty body only replays a committed admission.",
    ),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return admit(db, actor.id, idempotency_key, incident_id=incident_id, run=body)


@router.post("/{run_id}/cancel", operation_id="cancel_run", response_model=RunView)
def cancel(
    incident_id: UUID,
    run_id: UUID,
    body: CancelRequest | None = Body(default=None),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return cancel_run(db, incident_id, run_id, actor.id, body.reason if body else None)


@router.get(
    "", operation_id="list_runs", response_model=RunPage, dependencies=[Depends(current_session)]
)
def runs(
    incident_id: UUID,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    get_incident(db, incident_id)
    return list_runs(db, incident_id, limit, offset)


@router.get(
    "/{run_id}",
    operation_id="get_run",
    response_model=RunView,
    dependencies=[Depends(current_session)],
)
def run(incident_id: UUID, run_id: UUID, db: Session = Depends(get_db)):
    return get_run(db, incident_id, run_id)
