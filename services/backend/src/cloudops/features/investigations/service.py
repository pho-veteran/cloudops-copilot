from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.schemas import AttemptView, EvidenceView, RunPage, RunView
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt


def admit_run(db: Session, incident_id: UUID, actor_id: UUID) -> InvestigationRun:
    active = db.scalar(
        select(InvestigationRun.id).where(
            InvestigationRun.incident_id == incident_id,
            InvestigationRun.state.in_(["queued", "collecting"]),
        )
    )
    if active:
        raise DomainError(
            409,
            "run_active",
            "An investigation is already queued or collecting. Inspect it before starting another.",
        )
    run = InvestigationRun(incident_id=incident_id, requested_by=actor_id)
    db.add(run)
    db.flush()
    db.add(Job(kind="development_collection_v1", subject_id=run.id))
    return run


def run_view(db: Session, row: InvestigationRun) -> RunView:
    evidence = db.scalars(select(Evidence).where(Evidence.run_id == row.id)).all()
    attempts = db.scalars(
        select(JobAttempt)
        .join(Job)
        .where(Job.subject_id == row.id, Job.kind == "development_collection_v1")
        .order_by(JobAttempt.number)
    ).all()
    return RunView(
        **{
            field: getattr(row, field)
            for field in [
                "id",
                "incident_id",
                "state",
                "reason",
                "created_at",
                "finished_at",
                "requested_by",
            ]
        },
        evidence=[EvidenceView.model_validate(e) for e in evidence],
        attempts=[AttemptView.model_validate(a) for a in attempts],
    )


def get_run(db: Session, incident_id: UUID, run_id: UUID):
    row = db.get(InvestigationRun, run_id)
    if not row or row.incident_id != incident_id:
        raise DomainError(404, "run_not_found", "Investigation not found for this incident.")
    return run_view(db, row)


def list_runs(db: Session, incident_id: UUID, limit: int, offset: int):
    rows = db.scalars(
        select(InvestigationRun)
        .where(InvestigationRun.incident_id == incident_id)
        .order_by(InvestigationRun.created_at.desc(), InvestigationRun.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return RunPage(
        items=[run_view(db, r) for r in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )
