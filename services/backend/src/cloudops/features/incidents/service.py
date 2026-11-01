import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.incidents.models import Admission, Incident, IncidentActivity
from cloudops.features.incidents.schemas import (
    AdmissionView,
    IncidentDetail,
    IncidentPage,
    IncidentView,
    ManualIncidentRequest,
)
from cloudops.features.investigations import admission_settings
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.promotion import deferred_count, jobs_full
from cloudops.features.investigations.schemas import RunRequest, WindowPreset, WindowSpec
from cloudops.features.investigations.service import (
    Origin,
    Request,
    admit_run,
    run_view,
    terminalize,
    window_view,
)
from cloudops.features.workloads.service import identity, require_active
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError

NEW_REQUEST_FIELDS = {"question", "window"}
AUTOMATIC_WINDOW = timedelta(minutes=30)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def legacy_create_signature(body: ManualIncidentRequest) -> str:
    """Byte-identical to the committed three-field signature; new fields never join it."""
    return _digest(
        {
            "body": {
                "workload_id": str(body.workload_id),
                "title": body.title,
                "description": body.description,
            },
            "incident_id": None,
        }
    )


def legacy_run_signature(incident_id: UUID) -> str:
    return _digest({"body": None, "incident_id": str(incident_id)})


def canonical_window(spec: WindowSpec) -> dict:
    if isinstance(spec, WindowPreset):
        return {"preset": spec.preset}
    return {
        "start": spec.start.astimezone(timezone.utc).isoformat(),
        "end": spec.end.astimezone(timezone.utc).isoformat(),
    }


def request_signature(kind: str, **fields) -> str:
    return _digest({"v": 2, "kind": kind, **fields})


def resolve_window(spec: WindowSpec | None, now: datetime, default_source: str):
    """Resolve once at first admission. A replay returns the stored absolute window."""
    if spec is None:
        return now - timedelta(minutes=30), now, default_source
    if isinstance(spec, WindowPreset):
        return now - timedelta(minutes=spec.preset), now, f"preset_{spec.preset}"
    start = spec.start.astimezone(timezone.utc)
    end = spec.end.astimezone(timezone.utc)
    if end > now:
        raise DomainError(422, "invalid_window", "The window end cannot be in the future.")
    if not timedelta(minutes=1) <= end - start <= timedelta(minutes=60):
        raise DomainError(
            422,
            "invalid_window",
            "Choose a window that starts before it ends and spans 1 to 60 minutes.",
        )
    return start, end, "explicit"


def admission_view(db: Session, incident_id: UUID, run_id: UUID) -> AdmissionView:
    """Original identities with the Run's current persisted state."""
    run = db.get(InvestigationRun, run_id)
    return AdmissionView(
        incident_id=incident_id,
        run_id=run_id,
        state=run.state,
        queue_reason=run.queue_reason,
        window=window_view(run),
    )


def admit(
    db: Session,
    actor_id: UUID,
    key: UUID,
    body: ManualIncidentRequest | None = None,
    incident_id: UUID | None = None,
    run: RunRequest | None = None,
):
    """Manual create or attach. A full dispatch queue is a 409; manual work is never deferred."""
    if body is not None:
        legacy = not (body.model_fields_set & NEW_REQUEST_FIELDS)
        signature = (
            legacy_create_signature(body)
            if legacy
            else request_signature(
                "create",
                workload_id=str(body.workload_id),
                title=body.title,
                description=body.description,
                question=body.question,
                window=canonical_window(body.window) if body.window else None,
            )
        )
    else:
        legacy = run is None
        signature = (
            legacy_run_signature(incident_id)
            if legacy
            else request_signature(
                "run",
                incident_id=str(incident_id),
                workload_id=str(run.workload_id),
                question=run.reason,
                window=canonical_window(run.window),
            )
        )
    with db.begin():
        # Serialize bounded admission and deduplication in one short transaction.
        lock_admission(db)
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
            return admission_view(db, prior.incident_id, prior.run_id)
        if body is None and run is None:
            raise DomainError(
                422,
                "run_reason_required",
                "Describe why a new investigation is needed and review its time window.",
            )
        now = database_now(db)
        if body is not None:
            start, end, source = resolve_window(
                body.window, now, "legacy_default" if legacy else "preset_30"
            )
            question, question_source = (
                (body.question, "operator") if body.question else (body.title, "title_default")
            )
        else:
            start, end, source = resolve_window(run.window, now, "preset_30")
            question, question_source = run.reason, "operator"
        if jobs_full(db):
            raise DomainError(
                409,
                "queue_full",
                "The pending collection queue is full. Try again after existing work finishes.",
            )
        if body is not None:
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
            if incident.state == "resolved":
                raise DomainError(
                    409,
                    "incident_resolved",
                    "Reopen this incident before requesting a new investigation.",
                )
            if run.workload_id != incident.workload_id:
                raise DomainError(
                    409,
                    "target_mismatch",
                    "A new investigation must use the incident's own workload identity.",
                )
            require_active(db, incident.workload_id)
        row = admit_run(
            db,
            incident.id,
            Origin.operator(actor_id),
            Request(question, question_source, start, end, source),
        )
        db.add(
            Admission(
                actor_id=actor_id,
                key=key,
                request_hash=signature,
                incident_id=incident.id,
                run_id=row.id,
                signature_version=1 if legacy else 2,
            )
        )
        return admission_view(db, incident.id, row.id)


@dataclass(frozen=True)
class AutomaticRequest:
    workload_id: UUID
    title: str
    description: str
    question: str


def admit_automatic(
    db: Session,
    origin: Origin,
    request: AutomaticRequest,
    *,
    lookup: Callable[[Session], tuple[UUID, UUID] | None],
    bind: Callable[[Session, AdmissionView], None],
) -> AdmissionView:
    """Admit one trusted automatic request inside the caller's open transaction.

    ``lookup`` and ``bind`` belong to the caller (for example an alert episode store). An
    accepted identity is returned before any capacity check, with its current Run state.
    Nothing here commits or calls the network; any failure rolls back the caller's work.
    """
    if origin.kind != "alertmanager":
        raise ValueError("Automatic admission requires a trusted automatic origin.")
    lock_admission(db)
    prior = lookup(db)
    if prior:
        return admission_view(db, *prior)
    require_active(db, request.workload_id)
    deferred = False
    if jobs_full(db):
        if deferred_count(db) >= admission_settings.deferred_run_limit():
            raise DomainError(
                503,
                "capacity_exhausted",
                "Investigation capacity is exhausted. Retry the delivery later; nothing was admitted.",
            )
        deferred = True
    now = database_now(db)
    incident = Incident(
        workload_id=request.workload_id,
        title=request.title,
        description=request.description,
        created_by=None,
        origin_kind=origin.kind,
        origin_source_id=origin.source_id,
    )
    db.add(incident)
    db.flush()
    row = admit_run(
        db,
        incident.id,
        origin,
        Request(request.question, "automatic", now - AUTOMATIC_WINDOW, now, "automatic_default"),
        deferred=deferred,
    )
    view = admission_view(db, incident.id, row.id)
    bind(db, view)
    return view


def cancel_run(db: Session, incident_id: UUID, run_id: UUID, actor_id: UUID, reason: str | None):
    with db.begin():
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if not incident:
            raise DomainError(404, "incident_not_found", "Incident not found.")
        row = db.scalar(
            select(InvestigationRun)
            .where(InvestigationRun.id == run_id, InvestigationRun.incident_id == incident_id)
            .with_for_update()
        )
        if not row:
            raise DomainError(404, "run_not_found", "Investigation not found for this incident.")
        if row.state == "cancelled":
            return run_view(db, row)
        if row.state != "queued":
            raise DomainError(
                409,
                "run_not_cancellable",
                "Collection already started or finished; its result remains part of the history.",
            )
        terminalize(db, row, "operator_cancelled")
        db.add(
            IncidentActivity(
                incident_id=incident_id,
                kind="run_cancelled",
                text=reason or "Cancelled a queued investigation.",
                actor_id=actor_id,
            )
        )
        db.flush()
        return run_view(db, row)


def cancel_pending(db: Session, incident_id: UUID, code: str) -> None:
    """Terminalize queued Runs of a locked incident; collecting work is left to finish."""
    for row in db.scalars(
        select(InvestigationRun)
        .where(InvestigationRun.incident_id == incident_id, InvestigationRun.state == "queued")
        .with_for_update()
    ):
        terminalize(db, row, code)


def get_incident(db: Session, incident_id: UUID):
    row = db.get(Incident, incident_id)
    if not row:
        raise DomainError(404, "incident_not_found", "Incident not found.")
    return IncidentDetail(
        **IncidentView.model_validate(row).model_dump(), workload=identity(db, row.workload_id)
    )


def list_incidents(db: Session, limit: int, offset: int, workload_id: UUID | None = None):
    query = select(Incident)
    if workload_id is not None:
        identity(db, workload_id)
        query = query.where(Incident.workload_id == workload_id)
    rows = db.scalars(
        query.order_by(Incident.created_at.desc(), Incident.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return IncidentPage(
        items=[IncidentView.model_validate(r) for r in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )
