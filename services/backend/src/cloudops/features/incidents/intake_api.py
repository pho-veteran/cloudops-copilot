from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from cloudops.features.incidents import intake_service as service
from cloudops.features.incidents.intake_schemas import IntakeResult
from cloudops.shared.errors import ERROR_RESPONSES, ErrorEnvelope

router = APIRouter(
    tags=["intake"],
    responses={**ERROR_RESPONSES, 413: {"model": ErrorEnvelope}, 415: {"model": ErrorEnvelope}},
)


@router.post(
    "/api/intake/alertmanager",
    operation_id="receive_alertmanager_notification",
    response_model=IntakeResult,
    summary="Receive one Alertmanager webhook v4 notification",
    description=(
        "Machine-authenticated by the configured sender bearer token, never by a session. "
        "The body is read only after authentication and is bounded to 1 MiB and 100 alerts. "
        "A 503 asks Alertmanager to retry; committed alerts deduplicate on retry."
    ),
)
async def receive_alertmanager_notification(request: Request):
    # No declared body parameter: FastAPI would read and parse it before authentication.
    config = service.authenticate(request)
    envelope = await service.read_envelope(request)
    return await run_in_threadpool(service.receive, config, envelope)
