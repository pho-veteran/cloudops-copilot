import json
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.schemas import DevelopmentWorkload, WorkloadPage, WorkloadView
from cloudops.shared.config import settings
from cloudops.shared.errors import DomainError


def sync_config(db: Session):
    entries = TypeAdapter(list[DevelopmentWorkload]).validate_python(
        json.loads(settings().workloads_file.read_text())
    )
    if len(entries) > 100:
        raise ValueError("Configure at most 100 development workloads.")
    identities = [(e.cluster, e.namespace, e.name, e.uid) for e in entries]
    if len(set(identities)) != len(identities):
        raise ValueError("Development workload identities must be unique.")
    with db.begin():
        db.execute(update(Workload).values(active=False))
        for entry in entries:
            row = db.scalar(select(Workload).filter_by(**entry.model_dump()))
            if row:
                row.active = True
            else:
                db.add(Workload(**entry.model_dump()))


def require_active(db: Session, workload_id: UUID) -> Workload:
    row = db.scalar(select(Workload).where(Workload.id == workload_id).with_for_update())
    if not row or not row.active:
        raise DomainError(
            422, "workload_unavailable", "Choose an active configured development workload."
        )
    return row


def identity(db: Session, workload_id: UUID) -> WorkloadView:
    row = db.get(Workload, workload_id)
    if not row:
        raise DomainError(404, "workload_not_found", "Workload not found.")
    return WorkloadView.model_validate(row)


def inventory(db: Session, limit: int, offset: int):
    rows = db.scalars(
        select(Workload)
        .where(Workload.active)
        .order_by(Workload.id)
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return WorkloadPage(
        items=[WorkloadView.model_validate(w) for w in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )
