from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import COOKIE, current_session, operator
from cloudops.features.access.service import token_hash
from cloudops.features.access.schemas import Principal, SessionView
from cloudops.features.recovery import plans as builders, policy, service
from cloudops.features.recovery.models import ActionPlan, Execution, Verification, RollbackWindow
from cloudops.features.recovery.outcomes import Outcome
from cloudops.features.recovery.verification_policy import VerificationProfile, configured_profiles
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
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
    origin: Literal["operator_manual", "analysis_advice", "inverse_rollback"] | None
    source_analysis_id: UUID | None
    forward_execution_id: UUID | None
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
    server_time: datetime
    review_expires_at: datetime


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
    outcome: Outcome
    created_at: datetime
    finished_at: datetime | None


class VerificationView(View):
    execution_id: UUID
    profile: dict
    profile_digest: str
    baseline: dict | None
    acknowledged_at: datetime | None
    deadline: datetime | None
    next_slot: int
    samples: dict
    result: dict
    finalized_at: datetime | None


class AvailabilityBlocker(BaseModel):
    id: UUID
    incident_id: UUID
    state: str
    holds_lock: bool
    outcome: Outcome


class RecoveryAvailability(BaseModel):
    workload_id: UUID
    uid: str
    observed_at: datetime
    target_authority: Literal["enabled", "quarantined", "unavailable"]
    application_state: Literal["ready_for_preflight", "blocked", "unavailable"]
    epoch: str | None
    reason: str
    qualified_templates: list[
        Literal["scale_deployment", "restore_deployment_template", "set_memory_profile"]
    ]
    blocking_executions: list[AvailabilityBlocker]
    blocking_executions_truncated: bool


@router.get(
    "/workloads/{workload_id}/recovery-availability",
    operation_id="read_recovery_availability",
    response_model=RecoveryAvailability,
    dependencies=[Depends(current_session)],
)
def recovery_availability(workload_id: UUID, db: Session = Depends(get_db)):
    from cloudops.features.recovery.availability import read

    return read(db, workload_id)


class InversePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rationale: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class RollbackView(BaseModel):
    forward_execution_id: UUID
    can_propose: bool
    reason: str
    eligible_from: datetime | None
    deadline: datetime | None
    inverse_execution_id: UUID | None


@router.get(
    "/executions/{execution_id}/rollback",
    operation_id="read_rollback_eligibility",
    response_model=RollbackView,
    dependencies=[Depends(current_session)],
)
def rollback_eligibility(execution_id: UUID, db: Session = Depends(get_db)):
    from cloudops.features.recovery import rollback

    if db.get(Execution, execution_id) is None:
        raise DomainError(404, "execution_not_found", "Execution not found.")
    window = db.get(RollbackWindow, execution_id)
    can_propose, reason = False, "No original rollback authority was recorded."
    try:
        from cloudops.features.incidents.models import Incident
        from cloudops.features.workloads.service import require_active

        forward_execution, original, window, forward = rollback.require_forward(db, execution_id)
        can_propose = window.inverse_execution_id is None
        if can_propose:
            if db.get(Incident, original.incident_id).state == "resolved":
                rollback.deny("Reopen the saved incident before a fresh inverse proposal.")
            workload = require_active(db, forward_execution.workload_id)
            authority = service.available(db, workload)
            if authority["epoch"] != forward.epoch:
                rollback.deny("Maintenance retired the original forward authority.")
        reason = (
            "Fresh target preflight and a separate inverse review/approval are required."
            if can_propose
            else "An inverse is already approved; inspect its independent outcome."
        )
    except DomainError as error:
        can_propose = False
        reason = error.message
    return RollbackView(
        forward_execution_id=execution_id,
        can_propose=can_propose,
        reason=reason,
        eligible_from=window.eligible_from if window else None,
        deadline=window.deadline if window else None,
        inverse_execution_id=window.inverse_execution_id if window else None,
    )


@router.post(
    "/executions/{execution_id}/inverse-plans",
    operation_id="propose_inverse_plan",
    response_model=PlanView,
    status_code=201,
)
def propose_inverse_plan(
    execution_id: UUID,
    body: InversePlanRequest,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    from cloudops.features.recovery import rollback

    return rollback.propose(db, execution_id, actor.id, body.rationale)


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
    execution_capability: Literal["manual_scale_only", "qualified_profiles"] = "manual_scale_only"
    verification_profiles: list[VerificationProfile] = Field(default_factory=list)


@router.get(
    "/recovery-policy",
    operation_id="get_recovery_policy",
    response_model=PolicyView,
    dependencies=[Depends(current_session)],
)
def recovery_policy():
    rules = policy.load()
    profiles = [
        profile
        for profile in configured_profiles()
        if rules
        and rules.verification_policy == profile.id
        and profile.workload_uid in rules.workload_uids
    ]
    return PolicyView(
        policy=rules,
        digest=rules.digest() if rules else None,
        execution_capability="qualified_profiles" if profiles else "manual_scale_only",
        verification_profiles=profiles,
    )


@router.get(
    "/incidents/{incident_id}/plans",
    operation_id="list_plans",
    response_model=list[PlanView],
    dependencies=[Depends(current_session)],
)
def plans(
    incident_id: UUID,
    offset: int = Query(default=0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(ActionPlan)
        .where(ActionPlan.incident_id == incident_id)
        .order_by(ActionPlan.created_at.desc(), ActionPlan.id.desc())
        .offset(offset)
        .limit(25)
    ).all()


@router.get("/plans/{plan_id}", operation_id="read_plan", response_model=ReviewedPlan)
def read_plan(
    plan_id: UUID, session: SessionView = Depends(current_session), db: Session = Depends(get_db)
):
    plan, token, valid, reason, server_time, review_expires_at = service.read(db, plan_id, session)
    return ReviewedPlan(
        plan=PlanView.model_validate(plan),
        review_token=token,
        valid=valid,
        reason=reason,
        server_time=server_time,
        review_expires_at=review_expires_at,
    )


@router.post("/plans/{plan_id}/decision", operation_id="decide_plan", response_model=PlanView)
def decide_plan(
    plan_id: UUID,
    body: Decision,
    request: Request,
    actor: Principal = Depends(operator),
    session: SessionView = Depends(current_session),
    db: Session = Depends(get_db),
):
    return service.decide(db, plan_id, session, body, binding=token_hash(request.cookies[COOKIE]))


@router.get(
    "/incidents/{incident_id}/executions",
    operation_id="list_executions",
    response_model=list[ExecutionView],
    dependencies=[Depends(current_session)],
)
def executions(
    incident_id: UUID,
    offset: int = Query(default=0, ge=0, le=100000),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(Execution)
        .join(ActionPlan, Execution.plan_id == ActionPlan.id)
        .where(ActionPlan.incident_id == incident_id)
        .order_by(Execution.created_at.desc(), Execution.id.desc())
        .offset(offset)
        .limit(25)
    ).all()


@router.get(
    "/executions/{execution_id}",
    operation_id="read_recovery_execution",
    response_model=ExecutionView,
    dependencies=[Depends(current_session)],
)
def read_execution(execution_id: UUID, db: Session = Depends(get_db)):
    row = db.get(Execution, execution_id)
    if row is None:
        raise DomainError(404, "execution_not_found", "Execution not found.")
    return row


@router.get(
    "/executions/{execution_id}/verification",
    operation_id="read_recovery_verification",
    response_model=VerificationView,
    dependencies=[Depends(current_session)],
)
def verification(execution_id: UUID, db: Session = Depends(get_db)):
    row = db.get(Verification, execution_id)
    if row is None:
        raise DomainError(
            404,
            "verification_not_found",
            "No qualified verification record exists for this execution.",
        )
    return row
