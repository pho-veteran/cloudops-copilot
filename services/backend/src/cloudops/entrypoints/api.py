from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from cloudops.shared.database import require_schema
from cloudops.shared.errors import DomainError, domain_error_handler, validation_error_handler


@asynccontextmanager
async def lifespan(app):
    require_schema()
    yield


def create_app():
    app = FastAPI(title="CloudOps Copilot", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    from cloudops.features.health.api import router

    app.include_router(router)
    return app


app = create_app()
