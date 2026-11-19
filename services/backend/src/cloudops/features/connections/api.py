from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.connections import service
from cloudops.features.connections.schemas import CheckBody, CheckStatus, ReceiptView
from cloudops.features.incidents.intake_schemas import AlertReceiptPage
from cloudops.features.incidents.intake_service import receipts_page
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(prefix="/api/connection-checks", tags=["connections"], responses=ERROR_RESPONSES)


class CheckView(ReceiptView):
    """Preserve the published receipt schema name for existing clients."""


@router.post("", operation_id="create_connection_check", response_model=CheckView, status_code=201)
def check(
    body: CheckBody,
    idempotency_key: UUID | None = Header(default=None),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return service.create(db, actor.id, body.component, idempotency_key)


@router.get(
    "",
    operation_id="list_connection_checks",
    response_model=list[CheckView],
    dependencies=[Depends(current_session)],
)
def checks(db: Session = Depends(get_db)):
    return service.history(db)


@router.get(
    "/status",
    operation_id="get_connection_check_status",
    response_model=list[CheckStatus],
    dependencies=[Depends(current_session)],
)
def status(db: Session = Depends(get_db)):
    return service.statuses(db)


alert_receipts = APIRouter(
    prefix="/api/alert-receipts", tags=["connections"], responses=ERROR_RESPONSES
)


@alert_receipts.get(
    "",
    operation_id="list_alert_receipts",
    response_model=AlertReceiptPage,
    dependencies=[Depends(current_session)],
)
def receipts(
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=256),
    db: Session = Depends(get_db),
):
    """Sanitized authenticated intake outcomes; unauthenticated deliveries are never stored."""
    return receipts_page(db, limit, cursor)
