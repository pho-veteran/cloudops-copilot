from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.schemas import (
    AttemptView,
    EvidenceView,
    OriginView,
    RequestWindow,
    RunPage,
    RunView,
)
from cloudops.features.investigations.states import COLLECTION_JOB, nonterminal_run
from cloudops.features.recovery.classification import blocking_execution
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt, retire_queued

DEFERRED_REASON = "Accepted; waiting for dispatch capacity. It starts automatically."
DISPATCH_REASON = "Waiting for a collection worker."
OUTCOME_REASONS = {
    "operator_cancelled": "An Operator cancelled this queued investigation; no further attempt will start.",
    "incident_resolved": "The incident was resolved; no further collection attempt will start.",
    "scope_retired": "The workload identity left configured scope; no replacement or further attempt will be used.",
    "execution_blocking": "A recovery execution blocks this investigation; no further collection attempt will start.",
}


@dataclass(frozen=True)
class Origin:
    """Assigned by authenticated server code only; never selected by an HTTP caller."""

    kind: Literal["operator", "alertmanager"]
    actor_id: UUID | None = None
    source_id: str | None = None

    @classmethod
    def operator(cls, actor_id: UUID) -> "Origin":
        return cls("operator", actor_id=actor_id)

    @classmethod
    def alertmanager(cls, source_id: str) -> "Origin":
        if not 1 <= len(source_id) <= 64:
            raise ValueError("A trusted source ID has 1 to 64 characters.")
        return cls("alertmanager", source_id=source_id)


@dataclass(frozen=True)
class Request:
    question: str
    question_source: str
    window_start: datetime
    window_end: datetime
    window_source: str


def admit_run(
    db: Session, incident_id: UUID, origin: Origin, request: Request, *, deferred=False
) -> InvestigationRun:
    """Create one Run (and its Job unless deferred) inside the caller's admission transaction."""
    from cloudops.features.recovery.service import invalidate

    if blocking_execution(db, incident_id):
        raise DomainError(
            409,
            "execution_active",
            "An active execution must reach a recorded outcome before a new investigation.",
        )
    invalidate(db, incident_id)
    if nonterminal_run(db, incident_id):
        raise DomainError(
            409,
            "run_active",
            "An investigation is already queued or collecting. Inspect it before starting another.",
        )
    run = InvestigationRun(
        incident_id=incident_id,
        requested_by=origin.actor_id,
        origin_kind=origin.kind,
        origin_source_id=origin.source_id,
        question=request.question,
        question_source=request.question_source,
        window_start=request.window_start,
        window_end=request.window_end,
        window_source=request.window_source,
        queue_reason="capacity" if deferred else None,
        reason=DEFERRED_REASON if deferred else DISPATCH_REASON,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == (
            "one_open_run_per_incident"
        ):
            raise DomainError(
                409,
                "run_active",
                "An investigation is already queued or collecting. Inspect it before starting another.",
            ) from None
        raise
    if not deferred:
        db.add(Job(kind=COLLECTION_JOB, subject_id=run.id))
    return run


def terminalize(db: Session, run: InvestigationRun, code: str) -> None:
    """Cancel pending work; the caller holds the Run row lock."""
    run.state, run.queue_reason, run.outcome_code = "cancelled", None, code
    run.reason = OUTCOME_REASONS[code]
    run.finished_at = database_now(db)
    retire_queued(db, COLLECTION_JOB, run.id, OUTCOME_REASONS[code])


def dispatch_blocker(db: Session, run: InvestigationRun) -> str | None:
    """Deterministic eligibility for promotion and claim; related rows are read, not locked."""
    from cloudops.features.incidents.models import Incident
    from cloudops.features.workloads.models import Workload

    incident = db.get(Incident, run.incident_id)
    if incident.state == "resolved":
        return "incident_resolved"
    workload = db.get(Workload, incident.workload_id)
    if not workload or not workload.active:
        return "scope_retired"
    if blocking_execution(db, run.incident_id):
        return "execution_blocking"
    return None


def window_view(row: InvestigationRun) -> RequestWindow | None:
    if row.window_start is None:
        return None
    return RequestWindow(start=row.window_start, end=row.window_end, source=row.window_source)


def run_view(db: Session, row: InvestigationRun) -> RunView:
    evidence = db.scalars(select(Evidence).where(Evidence.run_id == row.id)).all()
    attempts = db.scalars(
        select(JobAttempt)
        .join(Job)
        .where(Job.subject_id == row.id, Job.kind == COLLECTION_JOB)
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
                "question",
                "question_source",
                "queue_reason",
                "outcome_code",
            ]
        },
        origin=OriginView(kind=row.origin_kind, source_id=row.origin_source_id),
        window=window_view(row),
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
