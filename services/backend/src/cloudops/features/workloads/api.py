from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.workloads.observations import (
    list_observations,
    read_observation,
    request_refresh,
)
from cloudops.features.workloads.schemas import (
    ObservationRefreshView,
    WorkloadObservationPage,
    WorkloadObservationView,
    WorkloadPage,
)
from cloudops.features.workloads.service import inventory
from cloudops.shared.database import database_now, get_db
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


@router.get(
    "/{workload_id}/observation",
    operation_id="get_workload_observation",
    response_model=WorkloadObservationView,
)
def get_workload_observation(workload_id: UUID, db: Session = Depends(get_db)):
    return read_observation(db, workload_id, database_now(db))


@router.get(
    "/{workload_id}/observations",
    operation_id="list_workload_observations",
    response_model=WorkloadObservationPage,
)
def list_workload_observations(
    workload_id: UUID,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    return list_observations(db, workload_id, limit, offset)


@router.post(
    "/{workload_id}/observation-refresh",
    operation_id="request_workload_observation_refresh",
    response_model=ObservationRefreshView,
    status_code=202,
)
def request_workload_observation_refresh(
    workload_id: UUID,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return request_refresh(db, workload_id, actor.id)
