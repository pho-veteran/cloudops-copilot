from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.schemas import (
    AttemptView,
    CancelState,
    DispatchStatus,
    EvidenceContent,
    OriginView,
    RequestWindow,
    RunPage,
    RunProgress,
    RunView,
)
from cloudops.features.investigations.states import (
    COLLECTION_JOB,
    nonterminal_run,
)
from cloudops.features.investigations.storage import evidence_views
from cloudops.features.incidents.models import Incident
from cloudops.features.workloads.models import Workload
from cloudops.features.recovery.classification import blocking_execution
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt, JobCheckpoint, retire_queued

FINAL_STAGE = "finalize_collection"

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
    from cloudops.features.diagnosis.lifecycle import cancel_unsent

    cancel_unsent(db, run, code)
    run.state, run.queue_reason, run.outcome_code = "cancelled", None, code
    run.provider_call = None
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


# Display-only collection dispatch status. Entrypoints that mount the trusted permit register
# a reader; without one the status is not reported. It never grants or denies any operation.
_dispatch_status: Callable[[], tuple[bool, str | None]] | None = None


def configure_dispatch_status(reader: Callable[[], tuple[bool, str | None]] | None) -> None:
    global _dispatch_status
    _dispatch_status = reader


def cancel_state(row: InvestigationRun) -> CancelState:
    """The feature-declared cancellation boundary: only work with no call already sent."""
    if row.state == "cancelled":
        return CancelState(allowed=False, reason="already_cancelled")
    if row.state == "queued":
        return CancelState(allowed=True, reason="queued")
    if row.state == "waiting_for_quota":
        if row.provider_call == "none":
            return CancelState(allowed=True, reason="waiting_without_provider_call")
        return CancelState(allowed=False, reason="provider_call_in_flight")
    if row.state == "collecting":
        return CancelState(allowed=False, reason="collection_started")
    if row.state == "analyzing":
        if row.provider_call == "none":
            return CancelState(allowed=True, reason="waiting_without_provider_call")
        return CancelState(allowed=False, reason="provider_call_in_flight")
    return CancelState(allowed=False, reason="finished")


def _progress(row, job, attempts, checkpoints, evidence) -> RunProgress | None:
    current = [e for e in evidence if e.catalog_version == CATALOG_VERSION]
    if job is None and not current:
        return None
    latest = attempts[-1] if attempts else None
    reused = (
        sum(1 for c in checkpoints if c.attempt_number < latest.number and c.stage != FINAL_STAGE)
        if latest
        else 0
    )
    return RunProgress(
        sources_total=len(SOURCES),
        sources_committed=len(current),
        sources_reused=reused,
        finalized=row.pack_coverage is not None,
        attempt=latest.number if latest else None,
        max_attempts=job.max_attempts if job else 3,
    )


def run_view(db: Session, row: InvestigationRun) -> RunView:
    evidence = db.scalars(
        select(Evidence)
        .join(InvestigationRun, Evidence.run_id == InvestigationRun.id)
        .join(Incident, InvestigationRun.incident_id == Incident.id)
        .join(Workload, Incident.workload_id == Workload.id)
        .where(Evidence.run_id == row.id, Evidence.workload_uid == Workload.uid)
        .order_by(Evidence.source_key)
    ).all()
    job = db.scalar(select(Job).where(Job.subject_id == row.id, Job.kind == COLLECTION_JOB))
    attempts = (
        db.scalars(
            select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.number)
        ).all()
        if job
        else []
    )
    checkpoints = (
        db.scalars(select(JobCheckpoint).where(JobCheckpoint.job_id == job.id)).all() if job else []
    )
    per_attempt: dict[int, int] = {}
    for item in checkpoints:
        if item.stage != FINAL_STAGE:
            per_attempt[item.attempt_number] = per_attempt.get(item.attempt_number, 0) + 1
    dispatch = None
    if row.state in {"queued", "collecting"} and _dispatch_status is not None:
        allowed, reason = _dispatch_status()
        dispatch = DispatchStatus(status="allowed" if allowed else "paused", reason=reason)
    from cloudops.features.diagnosis.service import analysis_view

    return RunView(
        analysis=analysis_view(db, row.id).model_dump(),
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
        pack_coverage=row.pack_coverage,
        evidence=evidence_views(db, evidence),
        attempts=[
            AttemptView(
                number=a.number,
                state=a.state,
                started_at=a.started_at,
                finished_at=a.finished_at,
                error=a.error,
                sources_committed=per_attempt.get(a.number, 0)
                if a.permit_generation is not None
                else None,
            )
            for a in attempts
        ],
        progress=_progress(row, job, attempts, checkpoints, evidence),
        dispatch=dispatch,
        cancel=cancel_state(row),
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


def scoped_evidence(db: Session, incident_id: UUID, run_id: UUID, evidence_id: UUID) -> Evidence:
    row = db.scalar(
        select(Evidence)
        .join(InvestigationRun, Evidence.run_id == InvestigationRun.id)
        .join(Incident, InvestigationRun.incident_id == Incident.id)
        .join(Workload, Incident.workload_id == Workload.id)
        .where(
            Evidence.id == evidence_id,
            Evidence.run_id == run_id,
            InvestigationRun.incident_id == incident_id,
            Evidence.workload_uid == Workload.uid,
        )
    )
    if row is None:
        raise DomainError(404, "evidence_not_found", "Evidence not found for this investigation.")
    return row


def evidence_metadata(db: Session, incident_id: UUID, run_id: UUID, evidence_id: UUID):
    return evidence_views(db, [scoped_evidence(db, incident_id, run_id, evidence_id)])[0]


def evidence_content(db: Session, incident_id: UUID, run_id: UUID, evidence_id: UUID):
    row = scoped_evidence(db, incident_id, run_id, evidence_id)
    view = evidence_views(db, [row])[0]
    if view.content_status == "expired":
        raise DomainError(410, "evidence_expired", "Evidence content has expired.")
    if view.content_status == "unavailable":
        raise DomainError(409, "evidence_unavailable", "Evidence content is unavailable.")
    payload = row.payload
    if payload is None:
        raise DomainError(410, "evidence_expired", "Evidence content has expired.")
    return EvidenceContent(
        evidence_id=row.id,
        run_id=row.run_id,
        schema_version=row.schema_version,
        payload_kind=row.payload_kind,
        payload_version=row.payload_version,
        sha256=row.sha256,
        payload=payload,
    )
