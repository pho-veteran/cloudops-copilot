import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cloudops.features.incidents.models import Admission, Incident
from cloudops.features.incidents.schemas import (
    AdmissionView,
    IncidentDetail,
    IncidentPage,
    IncidentView,
    ManualIncidentRequest,
)
from cloudops.features.investigations.service import admit_run
from cloudops.features.workloads.service import identity, require_active
from cloudops.shared.config import settings
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job


def admit(
    db: Session,
    actor_id: UUID,
    key: UUID,
    body: ManualIncidentRequest | None = None,
    incident_id: UUID | None = None,
):
    signature = hashlib.sha256(
        json.dumps(
            {
                "body": body.model_dump(mode="json") if body else None,
                "incident_id": str(incident_id) if incident_id else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with db.begin():
        # Serialize bounded admission and deduplication in one short transaction.
        db.execute(text("SELECT pg_advisory_xact_lock(742001)"))
        prior = db.scalar(
            select(Admission).where(Admission.actor_id == actor_id, Admission.key == key)
        )
        if prior:
            if prior.request_hash != signature:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This request key was already used with different input.",
                )
            return AdmissionView(incident_id=prior.incident_id, run_id=prior.run_id)
        queued = db.scalar(
            select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))
        )
        if queued >= settings().queue_limit:
            raise DomainError(
                409,
                "queue_full",
                "The pending collection queue is full. Try again after existing work finishes.",
            )
        if body:
            require_active(db, body.workload_id)
            incident = Incident(
                workload_id=body.workload_id,
                title=body.title,
                description=body.description,
                created_by=actor_id,
            )
            db.add(incident)
            db.flush()
        else:
            incident = db.scalar(
                select(Incident).where(Incident.id == incident_id).with_for_update()
            )
            if not incident:
                raise DomainError(404, "incident_not_found", "Incident not found.")
            require_active(db, incident.workload_id)
        run = admit_run(db, incident.id, actor_id)
        db.add(
            Admission(
                actor_id=actor_id,
                key=key,
                request_hash=signature,
                incident_id=incident.id,
                run_id=run.id,
            )
        )
        return AdmissionView(incident_id=incident.id, run_id=run.id)


def get_incident(db: Session, incident_id: UUID):
    row = db.get(Incident, incident_id)
    if not row:
        raise DomainError(404, "incident_not_found", "Incident not found.")
    return IncidentDetail(
        **IncidentView.model_validate(row).model_dump(), workload=identity(db, row.workload_id)
    )


def list_incidents(db: Session, limit: int, offset: int, workload_uuid: UUID | None = None):
    query = select(Incident)
    if workload_uuid is not None:
        # Filter on the exact persisted workload UUID before pagination so
        # replacement identities never leak into related-incident pages.
        query = query.where(Incident.workload_id == workload_uuid)
    rows = db.scalars(
        query.order_by(Incident.created_at.desc(), Incident.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return IncidentPage(
        items=[IncidentView.model_validate(r) for r in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )
