import os

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.health.schemas import HealthObservation, HealthView
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, get_db, require_schema
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.features.workloads.models import Workload
from cloudops.features.recovery.client import target

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
    modes = {
        mode.strip()
        for mode in os.environ.get("COLLECTION_SOURCE_MODE", "none").split(",")
        if mode.strip()
    }
    if "prometheus" in modes:
        collector_status, collector_source, collector_reason = (
            "available",
            "prometheus",
            "Scoped Prometheus metric collection is configured. Availability does not "
            "indicate collection success or a live diagnosis, and Kubernetes sources remain "
            "separate. Inspect investigation attempts for results.",
        )
    elif "development_fixture" in modes:
        collector_status, collector_source, collector_reason = (
            "available",
            "development_fixture",
            "Synthetic collector adapter configured; availability does not indicate "
            "collection success. Inspect investigation attempts for results. "
            "No live Kubernetes or Prometheus source adapter is installed.",
        )
    else:
        collector_status, collector_source, collector_reason = (
            "unavailable",
            "unimplemented",
            "No collection source adapter is configured. Runs record missing-source "
            "coverage; no live Kubernetes or Prometheus telemetry is collected.",
        )
    executor_status, executor_source, executor_reason = (
        "unavailable",
        "unimplemented",
        "No isolated executor is configured; application roles cannot enable product writes.",
    )
    if os.environ.get("EXECUTOR_URL"):
        executor_source, executor_reason = (
            "live_gateway",
            "Executor target authority is unavailable or quarantined. Use trusted deployment maintenance, not application credentials.",
        )
        row = db.scalar(
            select(Workload).where(Workload.active, Workload.source == "live_kubernetes").limit(1)
        )
        if row:
            try:
                authority = target(row.uid)
                if authority["enabled"] and not authority["recovery_mode"]:
                    executor_status, executor_reason = (
                        "available",
                        "A scoped target is enabled. Every exact workload still requires current authority, live prerequisites and explicit approval. Qualified post-checks are separate from acknowledgement; inspect its recovery availability and execution evidence.",
                    )
            except DomainError:
                pass

    def observation(component, status, source, reason, last_success=None):
        return HealthObservation(
            component=component,
            status=status,
            source=source,
            observed_at=now,
            last_success_at=last_success,
            reason=reason,
        )

    from cloudops.features.diagnosis.service import model_status

    model = model_status(db)
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
                collector_status,
                collector_source,
                collector_reason,
            ),
            observation(
                "diagnosis",
                "available"
                if model.availability == "available" and model.dispatch_allowed
                else "unavailable",
                "runtime",
                "Bounded analysis: "
                + model.availability
                + ". Quality and transport checks are separate.",
            ),
            observation(
                "executor",
                executor_status,
                executor_source,
                executor_reason,
            ),
        ]
    )
