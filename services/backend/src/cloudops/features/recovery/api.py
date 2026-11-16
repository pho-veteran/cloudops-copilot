from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal, SessionView
from cloudops.features.recovery import plans as builders, policy, service
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api", tags=["recovery"], responses=ERROR_RESPONSES)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Literal["operator_manual"] = "operator_manual"
    replicas: int = Field(strict=True, ge=1, le=3)
    rationale: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class AdvicePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Literal["analysis_advice"]
    run_id: UUID
    analysis_id: UUID
    template: Literal["restore_deployment_template", "set_memory_profile", "scale_deployment"]
    baseline_id: UUID | None = None
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    replicas: int | None = Field(default=None, strict=True, ge=1, le=3)
    rationale: str = Field(min_length=1, max_length=2000, pattern=r"\S")

    @model_validator(mode="after")
    def exact_selector(self):
        selected = {
            "restore_deployment_template": "baseline_id",
            "set_memory_profile": "profile_id",
            "scale_deployment": "replicas",
        }[self.template]
        for field in ("baseline_id", "profile_id", "replicas"):
            if field != selected and getattr(self, field) is not None:
                raise ValueError("Supply only the selector for the exact template.")
        if self.template == "scale_deployment" and self.replicas is None:
            raise ValueError("An absolute replica count is required.")
        return self


class ManualOnly(BaseModel):
    state: Literal["manual_only"]
    reason: str
    next_step: str


class PlanView(View):
    id: UUID
    incident_id: UUID
    workload_id: UUID
    version: int
    template: Literal["scale_deployment", "restore_deployment_template", "set_memory_profile"]
    origin: Literal["operator_manual", "analysis_advice"] | None
    source_analysis_id: UUID | None
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
    response_model=PlanView | ManualOnly,
    status_code=201,
)
def propose(
    incident_id: UUID,
    body: PlanRequest | AdvicePlanRequest,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    if isinstance(body, AdvicePlanRequest):
        return builders.propose(db, incident_id, actor.id, body)
    return service.propose(db, incident_id, actor.id, body.replicas, body.rationale)


class PolicyView(BaseModel):
    policy: policy.RecoveryPolicy | None
    digest: str | None
    execution_capability: Literal["manual_scale_only"] = "manual_scale_only"


@router.get(
    "/recovery-policy",
    operation_id="get_recovery_policy",
    response_model=PolicyView,
    dependencies=[Depends(current_session)],
)
def recovery_policy():
    rules = policy.load()
    return PolicyView(policy=rules, digest=rules.digest() if rules else None)


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
