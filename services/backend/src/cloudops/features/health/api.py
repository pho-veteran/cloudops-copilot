from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.health.schemas import HealthObservation, HealthView
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, get_db, require_schema
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
    now = database_now(db)
    beat = db.get(WorkerHeartbeat, "collector")
    stale = not beat or (now - beat.observed_at).total_seconds() > settings().worker_stale_seconds

    def observation(component, status, source, reason, last_success=None):
        return HealthObservation(
            component=component,
            status=status,
            source=source,
            observed_at=now,
            last_success_at=last_success,
            reason=reason,
        )

    return HealthView(
        observations=[
            observation(
                "database", "available", "runtime", "Connected to the application database.", now
            ),
            observation(
                "worker",
                "stale" if stale else "available",
                "runtime",
                "No fresh worker heartbeat. Start the worker; interrupted collection is reclaimed after its 30-second lease."
                if stale
                else "Collection worker heartbeat is fresh.",
                beat.observed_at if beat else None,
            ),
            observation(
                "collector",
                "available",
                "development_fixture",
                "Deterministic synthetic collector; live Kubernetes/Prometheus unavailable.",
            ),
            observation(
                "diagnosis",
                "unavailable",
                "unimplemented",
                "AI diagnosis is unavailable until E06.",
            ),
            observation(
                "executor",
                "unavailable",
                "unimplemented",
                "Recovery is unavailable until E07; executor boundary disabled.",
            ),
        ]
    )
