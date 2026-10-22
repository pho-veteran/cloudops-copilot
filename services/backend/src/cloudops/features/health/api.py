from fastapi import APIRouter

from cloudops.shared.database import require_schema
from cloudops.shared.errors import DomainError, ERROR_RESPONSES

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
