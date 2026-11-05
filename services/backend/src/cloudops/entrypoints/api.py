import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from cloudops.features.incidents.intake_settings import intake_config
from cloudops.features.investigations.admission_settings import deferred_run_limit
from cloudops.features.investigations.collector import configured_adapters
from cloudops.shared.config import settings
from cloudops.shared.database import require_schema
from cloudops.shared.errors import DomainError, domain_error_handler, validation_error_handler


@asynccontextmanager
async def lifespan(app):
    settings()
    deferred_run_limit()
    intake_config()
    configured_adapters(os.environ.get("COLLECTION_SOURCE_MODE", "none"))
    require_schema()
    yield


def create_app():
    app = FastAPI(title="CloudOps Copilot", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, exc):
        return await domain_error_handler(
            request,
            DomainError(
                503,
                "database_unavailable",
                "Application database unavailable. Check the database service, then retry explicitly.",
            ),
        )

    from cloudops.features.access.api import router as access
    from cloudops.features.health.api import router as health
    from cloudops.features.incidents.api import router as incidents
    from cloudops.features.investigations.api import router as investigations
    from cloudops.features.workloads.api import router as workloads
    from cloudops.features.workloads.baselines import router as baselines
    from cloudops.features.incidents.lifecycle import router as lifecycle
    from cloudops.features.incidents.intake_api import router as intake
    from cloudops.features.connections.api import alert_receipts
    from cloudops.features.connections.api import router as connections
    from cloudops.features.recommendations.api import router as recommendations
    from cloudops.features.recovery.api import router as recovery
    from cloudops.features.recovery.baseline_context import router as recovery_context

    for router in (
        health,
        access,
        workloads,
        incidents,
        investigations,
        baselines,
        lifecycle,
        intake,
        connections,
        alert_receipts,
        recommendations,
        recovery,
        recovery_context,
    ):
        app.include_router(router)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()
