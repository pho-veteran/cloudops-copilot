import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.workloads.kubernetes import Kubernetes, healthy, snapshot
from cloudops.features.workloads.models import Baseline, BaselineCandidate
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now, get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api/workloads", tags=["baselines"], responses=ERROR_RESPONSES)


class CandidateView(View):
    id: UUID
    workload_id: UUID
    digest: str
    snapshot: dict
    created_at: datetime


class BaselineView(View):
    id: UUID
    workload_id: UUID
    candidate_id: UUID
    digest: str
    snapshot: dict
    confirmed_by: UUID
    confirmed_at: datetime


class ConfirmBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def observe(workload):
    if workload.source != "live_kubernetes":
        raise DomainError(
            409,
            "baseline_unavailable",
            "Synthetic evidence cannot establish a known-good baseline.",
        )
    deployment = Kubernetes().deployment(workload)
    if not healthy(deployment):
        raise DomainError(
            409,
            "baseline_unhealthy",
            "The live Deployment is not fully ready and stable; baseline confirmation is unavailable.",
        )
    value = snapshot(deployment)
    return value, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@router.get(
    "/{workload_id}/baseline-candidate",
    operation_id="get_baseline_candidate",
    response_model=CandidateView,
    dependencies=[Depends(current_session)],
)
def candidate(workload_id: UUID, db: Session = Depends(get_db)):
    with db.begin():
        workload = require_active(db, workload_id)
        value, digest = observe(workload)
        now = database_now(db)
        row = db.scalar(
            select(BaselineCandidate)
            .where(
                BaselineCandidate.workload_id == workload_id,
                BaselineCandidate.digest == digest,
                BaselineCandidate.created_at >= now - timedelta(minutes=5),
            )
            .order_by(BaselineCandidate.created_at.desc())
            .limit(1)
        )
        if not row:
            row = BaselineCandidate(workload_id=workload_id, digest=digest, snapshot=value)
            db.add(row)
            db.flush()
        return CandidateView.model_validate(row)


@router.post(
    "/{workload_id}/baseline",
    operation_id="confirm_baseline",
    response_model=BaselineView,
    status_code=201,
)
def confirm(
    workload_id: UUID,
    body: ConfirmBaseline,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    with db.begin():
        workload = require_active(db, workload_id)
        row = db.get(BaselineCandidate, body.candidate_id)
        now = database_now(db)
        if (
            not row
            or row.workload_id != workload_id
            or row.digest != body.digest
            or not timedelta(seconds=5) <= now - row.created_at <= timedelta(minutes=5)
        ):
            raise DomainError(
                409,
                "candidate_changed",
                "Read a healthy candidate, observe it for at least five seconds and confirm within five minutes.",
            )
        value, digest = observe(workload)
        if digest != row.digest:
            raise DomainError(
                409,
                "candidate_changed",
                "The candidate changed; inspect a fresh snapshot before confirmation.",
            )
        prior = db.scalar(select(Baseline).where(Baseline.candidate_id == row.id))
        if prior:
            return BaselineView.model_validate(prior)
        baseline = Baseline(
            workload_id=workload_id,
            candidate_id=row.id,
            digest=digest,
            snapshot=value,
            confirmed_by=actor.id,
        )
        db.add(baseline)
        db.flush()
        return BaselineView.model_validate(baseline)


@router.get(
    "/{workload_id}/baselines",
    operation_id="list_baselines",
    response_model=list[BaselineView],
    dependencies=[Depends(current_session)],
)
def baselines(workload_id: UUID, db: Session = Depends(get_db)):
    return db.scalars(
        select(Baseline)
        .where(Baseline.workload_id == workload_id)
        .order_by(Baseline.confirmed_at.desc())
        .limit(25)
    ).all()
