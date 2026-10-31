from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents import service
from cloudops.features.incidents.schemas import (
    AdmissionView,
    IncidentDetail,
    IncidentPage,
    ManualIncidentRequest,
)
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(prefix="/api/incidents", tags=["incidents"], responses=ERROR_RESPONSES)


@router.post("", operation_id="create_incident", response_model=AdmissionView, status_code=201)
def create_incident(
    body: ManualIncidentRequest,
    idempotency_key: UUID = Header(),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return service.admit(db, actor.id, idempotency_key, body=body)


@router.get(
    "",
    operation_id="list_incidents",
    response_model=IncidentPage,
    dependencies=[Depends(current_session)],
)
def list_incidents(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    workload_id: UUID | None = None,
    db: Session = Depends(get_db),
):
    return service.list_incidents(db, limit, offset, workload_id)


@router.get(
    "/{incident_id}",
    operation_id="get_incident",
    response_model=IncidentDetail,
    dependencies=[Depends(current_session)],
)
def get_incident(incident_id: UUID, db: Session = Depends(get_db)):
    return service.get_incident(db, incident_id)
