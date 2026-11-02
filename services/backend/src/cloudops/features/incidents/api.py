from typing import Literal
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
    search: str | None = Query(None, max_length=160, description="Search in title or ID"),
    workload_id: UUID | None = Query(None, description="Filter by workload"),
    origin: Literal["operator", "alertmanager"] | None = Query(None),
    state: Literal["open", "acknowledged", "resolved"] | None = Query(None),
    severity: str | None = Query(None, max_length=32, description="Filter by severity value"),
    alert_signal: Literal["none", "firing", "resolved"] | None = Query(None),
    sort_by: Literal["created_at", "first_seen_at", "last_seen_at", "severity"] = Query(
        "created_at"
    ),
    sort_order: Literal["asc", "desc"] = Query("desc"),
    db: Session = Depends(get_db),
):
    return service.list_incidents(
        db,
        limit,
        offset,
        search=search,
        filters={
            "workload_id": workload_id,
            "origin": origin,
            "state": state,
            "severity": severity,
            "alert_signal": alert_signal,
        },
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get(
    "/{incident_id}",
    operation_id="get_incident",
    response_model=IncidentDetail,
    dependencies=[Depends(current_session)],
)
def get_incident(incident_id: UUID, db: Session = Depends(get_db)):
    return service.get_incident(db, incident_id)
