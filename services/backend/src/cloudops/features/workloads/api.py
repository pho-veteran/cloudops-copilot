from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.workloads.schemas import WorkloadPage, WorkloadView
from cloudops.features.workloads.service import inventory, identity
from cloudops.features.workloads.observation_schemas import ObservationDetail, ObservationPage
from cloudops.features.workloads.observation_service import detail, history
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(
    prefix="/api/workloads",
    tags=["workloads"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(current_session)],
)


@router.get("", operation_id="list_workloads", response_model=WorkloadPage)
def list_workloads(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    return inventory(db, limit, offset)


@router.get("/{workload_id}", operation_id="get_workload", response_model=WorkloadView)
def get_workload(workload_id: UUID, db: Session = Depends(get_db)):
    return identity(db, workload_id)


@router.get(
    "/{workload_id}/observation",
    operation_id="get_workload_observation",
    response_model=ObservationDetail,
)
def get_workload_observation(workload_id: UUID, db: Session = Depends(get_db)):
    return detail(db, workload_id)


@router.get(
    "/{workload_id}/observations",
    operation_id="list_workload_observations",
    response_model=ObservationPage,
)
def list_workload_observations(
    workload_id: UUID,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=256),
    db: Session = Depends(get_db),
):
    return history(db, workload_id, limit, cursor)
