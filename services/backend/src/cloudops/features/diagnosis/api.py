from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.diagnosis import service
from cloudops.features.diagnosis.settings import load
from cloudops.features.diagnosis.api_schemas import (
    AnalysisView,
    CheckPage,
    CheckView,
    EmptyRequest,
    ModelStatus,
)
from cloudops.features.diagnosis.models import ModelCheck
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(tags=["diagnosis"], responses=ERROR_RESPONSES)
RUN = "/api/incidents/{incident_id}/runs/{run_id}/analysis"


@router.get(
    RUN,
    operation_id="get_analysis",
    response_model=AnalysisView,
    dependencies=[Depends(current_session)],
)
def read_analysis(incident_id: UUID, run_id: UUID, db: Session = Depends(get_db)):
    service.scoped_run(db, incident_id, run_id)
    return service.analysis_view(db, run_id)


@router.post(RUN + "/resume", operation_id="resume_analysis", response_model=AnalysisView)
def resume_analysis(
    incident_id: UUID,
    run_id: UUID,
    idempotency_key: UUID = Header(),
    body: EmptyRequest | None = Body(default=None),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return service.resume(db, incident_id, run_id, actor.id, idempotency_key)


@router.get(
    "/api/diagnosis/status",
    operation_id="get_model_status",
    response_model=ModelStatus,
    dependencies=[Depends(current_session)],
)
def read_status(db: Session = Depends(get_db)):
    return service.model_status(db)


@router.get(
    "/api/diagnosis/checks",
    operation_id="list_model_checks",
    response_model=CheckPage,
    dependencies=[Depends(current_session)],
)
def read_checks(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(ModelCheck)
        .order_by(ModelCheck.created_at.desc(), ModelCheck.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    deployment = load()
    return CheckPage(
        items=[service.check_view(row, deployment) for row in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.post(
    "/api/diagnosis/checks",
    operation_id="create_model_check",
    response_model=CheckView,
    status_code=201,
)
def create_check(
    idempotency_key: UUID = Header(),
    body: EmptyRequest | None = Body(default=None),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return service.enqueue_check(db, actor.id, idempotency_key)
