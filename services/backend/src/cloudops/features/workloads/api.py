from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.workloads.schemas import WorkloadPage
from cloudops.features.workloads.service import inventory
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
