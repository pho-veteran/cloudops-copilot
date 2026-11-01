from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from cloudops.features.access.dependencies import COOKIE, current_session, operator
from cloudops.features.access.service import token_hash
from cloudops.features.workloads import baseline_service as service
from cloudops.features.workloads.baseline_schemas import (
    BaselinePage,
    BaselineView,
    CandidateView,
    ConfirmBaseline,
)
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(prefix="/api/workloads", tags=["baselines"], responses=ERROR_RESPONSES)


@router.get(
    "/{workload_id}/baseline-candidate",
    operation_id="get_baseline_candidate",
    response_model=CandidateView,
    dependencies=[Depends(current_session)],
)
async def candidate(workload_id: UUID, request: Request):
    return await service.candidate(workload_id, token_hash(request.cookies[COOKIE]))


@router.post(
    "/{workload_id}/baseline",
    operation_id="confirm_baseline",
    response_model=BaselineView,
    status_code=201,
    dependencies=[Depends(operator)],
)
async def confirm(workload_id: UUID, body: ConfirmBaseline, request: Request):
    return await service.confirm(workload_id, body, token_hash(request.cookies[COOKIE]))


@router.get(
    "/{workload_id}/baselines",
    operation_id="list_baselines",
    response_model=BaselinePage,
    dependencies=[Depends(current_session)],
)
def baselines(
    workload_id: UUID,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=256),
    db: Session = Depends(get_db),
):
    return service.history(db, workload_id, limit, cursor)
