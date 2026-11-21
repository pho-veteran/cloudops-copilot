from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.health.schemas import HealthView
from cloudops.features.health.service import project
from cloudops.shared.database import get_db, require_schema
from cloudops.shared.errors import ERROR_RESPONSES, DomainError

router = APIRouter()


@router.get("/api/health/live", operation_id="liveness", response_model=dict[str, str])
def live():
    return {"status": "available"}


@router.get(
    "/api/health/ready",
    operation_id="readiness",
    response_model=dict[str, str],
    responses=ERROR_RESPONSES,
)
def ready():
    try:
        require_schema()
    except Exception:
        raise DomainError(
            503,
            "database_not_ready",
            "Database unavailable or migration required. Run migrate, then restart API/worker.",
        ) from None
    return {"status": "available"}


@router.get(
    "/api/health",
    operation_id="get_health",
    response_model=HealthView,
    responses=ERROR_RESPONSES,
    dependencies=[Depends(current_session)],
)
def observations(db: Session = Depends(get_db)):
    return project(db)
