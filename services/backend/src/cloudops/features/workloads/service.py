from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.workloads.models import ScopeTarget, Workload
from cloudops.features.workloads.observation_service import summaries
from cloudops.features.workloads.schemas import (
    ScopePage,
    ScopeTargetView,
    WorkloadPage,
    WorkloadView,
)
from cloudops.shared.errors import DomainError


def sync_config(db: Session, previous_fingerprint=None):
    from cloudops.features.workloads.reconcile import sync

    return sync(db, previous_fingerprint)


def require_active(db: Session, workload_id: UUID) -> Workload:
    row = db.scalar(select(Workload).where(Workload.id == workload_id).with_for_update())
    if not row or not row.active:
        raise DomainError(
            422,
            "workload_unavailable",
            "Choose an active configured workload; historical identities cannot admit new work.",
        )
    return row


def identity(db: Session, workload_id: UUID) -> WorkloadView:
    row = db.get(Workload, workload_id)
    if not row:
        raise DomainError(404, "workload_not_found", "Workload not found.")
    return view(row, summaries(db, [row])[row.id])


def view(row, observation):
    return WorkloadView(
        **WorkloadView.model_validate(row).model_dump(exclude={"health", "observation"}),
        health=observation.effective_health,
        observation=observation,
    )


def inventory(db: Session, limit: int, offset: int):
    rows = db.scalars(
        select(Workload)
        .where(Workload.active)
        .order_by(Workload.id)
        .limit(limit + 1)
        .offset(offset)
    ).all()
    projected = summaries(db, rows[:limit])
    return WorkloadPage(
        items=[view(w, projected[w.id]) for w in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )


def scope_inventory(db, limit, offset, removed):
    rows = db.execute(
        select(ScopeTarget, Workload.uid)
        .outerjoin(Workload, ScopeTarget.workload_id == Workload.id)
        .where(ScopeTarget.configured == (not removed))
        .order_by(ScopeTarget.cluster, ScopeTarget.namespace, ScopeTarget.name, ScopeTarget.id)
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return ScopePage(
        items=[
            ScopeTargetView.model_validate(target).model_copy(update={"uid": uid})
            for target, uid in rows[:limit]
        ],
        next_offset=offset + limit if len(rows) > limit else None,
    )
