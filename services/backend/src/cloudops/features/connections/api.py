from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.connections.models import ConnectionCheck
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.models import Workload
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api/connection-checks", tags=["connections"], responses=ERROR_RESPONSES)


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Literal["cluster", "database", "worker"]


class CheckView(View):
    id: UUID
    component: str
    status: Literal["available", "unavailable", "stale"]
    reason: str
    source: str
    created_by: UUID
    observed_at: datetime


@router.post("", operation_id="create_connection_check", response_model=CheckView, status_code=201)
def check(body: CheckBody, actor: Principal = Depends(operator), db: Session = Depends(get_db)):
    with db.begin():
        now = database_now(db)
        status, source, reason = "available", "runtime", "Application database query succeeded."
        if body.component == "worker":
            beat = db.get(WorkerHeartbeat, "collector")
            status = (
                "available"
                if beat
                and (now - beat.observed_at).total_seconds() <= settings().worker_stale_seconds
                else "stale"
            )
            reason = (
                "Worker heartbeat is fresh."
                if status == "available"
                else "No fresh worker heartbeat; inspect the configured worker."
            )
        elif body.component == "cluster":
            workload = db.scalar(
                select(Workload)
                .where(Workload.active, Workload.source == "live_kubernetes")
                .order_by(Workload.id)
                .limit(1)
            )
            source = "live_kubernetes"
            try:
                if not workload:
                    raise DomainError(503, "cluster_unavailable", "No live target is configured.")
                Kubernetes().deployment(workload)
                reason = "Scoped authenticated Deployment read succeeded. This does not establish diagnosis or execution readiness."
            except DomainError:
                status, reason = (
                    "unavailable",
                    "Scoped cluster read failed or target identity changed. Check credentials, CA and allowlist.",
                )
        row = ConnectionCheck(
            component=body.component,
            status=status,
            source=source,
            reason=reason,
            created_by=actor.id,
        )
        db.add(row)
        db.flush()
        return CheckView.model_validate(row)


@router.get(
    "",
    operation_id="list_connection_checks",
    response_model=list[CheckView],
    dependencies=[Depends(current_session)],
)
def checks(db: Session = Depends(get_db)):
    return db.scalars(
        select(ConnectionCheck)
        .order_by(ConnectionCheck.observed_at.desc(), ConnectionCheck.id.desc())
        .limit(50)
    ).all()
