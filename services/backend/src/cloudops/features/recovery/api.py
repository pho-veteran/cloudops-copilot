from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal, SessionView
from cloudops.features.recovery import service
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api", tags=["recovery"], responses=ERROR_RESPONSES)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replicas: int = Field(strict=True, ge=1, le=3)
    rationale: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class PlanView(View):
    id: UUID
    incident_id: UUID
    workload_id: UUID
    version: int
    template: Literal["scale_deployment"]
    content: dict
    digest: str
    target_epoch: str
    state: Literal["proposed", "approved", "rejected", "invalidated"]
    rationale: str
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None


class ReviewedPlan(BaseModel):
    plan: PlanView
    review_token: str
    valid: bool
    reason: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_token: str = Field(min_length=16, max_length=128)
    decision: Literal["approve", "reject"]


class ExecutionView(View):
    id: UUID
    plan_id: UUID
    workload_id: UUID
    state: str
    holds_lock: bool
    result: dict
    created_at: datetime
    finished_at: datetime | None


@router.post(
    "/incidents/{incident_id}/plans",
    operation_id="propose_plan",
    response_model=PlanView,
    status_code=201,
)
def propose(
    incident_id: UUID,
    body: PlanRequest,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    return service.propose(db, incident_id, actor.id, body.replicas, body.rationale)


@router.get(
    "/incidents/{incident_id}/plans",
    operation_id="list_plans",
    response_model=list[PlanView],
    dependencies=[Depends(current_session)],
)
def plans(incident_id: UUID, db: Session = Depends(get_db)):
    return db.scalars(
        select(ActionPlan)
        .where(ActionPlan.incident_id == incident_id)
        .order_by(ActionPlan.created_at.desc())
        .limit(25)
    ).all()


@router.get("/plans/{plan_id}", operation_id="read_plan", response_model=ReviewedPlan)
def read_plan(
    plan_id: UUID, session: SessionView = Depends(current_session), db: Session = Depends(get_db)
):
    plan, token, valid, reason = service.read(db, plan_id, session)
    return ReviewedPlan(
        plan=PlanView.model_validate(plan), review_token=token, valid=valid, reason=reason
    )


@router.post("/plans/{plan_id}/decision", operation_id="decide_plan", response_model=PlanView)
def decide_plan(
    plan_id: UUID,
    body: Decision,
    actor: Principal = Depends(operator),
    session: SessionView = Depends(current_session),
    db: Session = Depends(get_db),
):
    return service.decide(db, plan_id, session, body)


@router.get(
    "/incidents/{incident_id}/executions",
    operation_id="list_executions",
    response_model=list[ExecutionView],
    dependencies=[Depends(current_session)],
)
def executions(incident_id: UUID, db: Session = Depends(get_db)):
    return db.scalars(
        select(Execution)
        .join(ActionPlan)
        .where(ActionPlan.incident_id == incident_id)
        .order_by(Execution.created_at.desc())
        .limit(25)
    ).all()
