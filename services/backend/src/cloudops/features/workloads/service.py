import json
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_service import summaries
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
        # A new explicit authority window fences in-flight reads, including same-UID
        # retirement/re-add. Scope synchronization remains the only admission writer.
        db.execute(
            update(Workload).values(active=False, observation_epoch=Workload.observation_epoch + 1)
        )
        for entry in entries:
            row = db.scalar(select(Workload).filter_by(**entry.model_dump(exclude={"source"})))
            if row:
                row.active = True
                row.source = entry.source
            else:
                db.add(Workload(**entry.model_dump()))


def require_active(db: Session, workload_id: UUID) -> Workload:
    row = db.scalar(select(Workload).where(Workload.id == workload_id).with_for_update())
    if not row or not row.active:
        raise DomainError(422, "workload_unavailable", "Choose an active configured workload.")
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
