import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.incidents.models import Admission, Incident, IncidentActivity
from cloudops.features.incidents.schemas import (
    AdmissionView,
    CreationAnchor,
    IncidentDetail,
    IncidentPage,
    IncidentView,
    ManualIncidentRequest,
    WorkQueueExecution,
    WorkQueueItem,
    WorkQueueRun,
)
from cloudops.features.access.models import User
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
from cloudops.features.recovery.classification import (
    classification_expression,
    classify as classify_execution,
)
from cloudops.features.recovery.models import ActionPlan, Execution
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


SORT_COLUMNS = {
    "created_at": Incident.created_at,
    "first_seen_at": Incident.first_seen_at,
    "last_seen_at": Incident.last_seen_at,
    "severity": Incident.severity,
}
NULLABLE_SORT_COLUMNS = {"first_seen_at", "last_seen_at", "severity"}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_incidents(
    db: Session,
    limit: int,
    offset: int,
    search: str | None = None,
    filters: dict | None = None,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
    view: str | None = None,
    run_state: str | None = None,
    execution_class: str | None = None,
    anchor_created_at: datetime | None = None,
    anchor_id: UUID | None = None,
):
    """Return one server-backed work-queue page with independent persisted status axes."""
    if sort_by not in SORT_COLUMNS:
        raise ValueError(f"Unknown incident sort column: {sort_by}")
    if (anchor_created_at is None) != (anchor_id is None):
        raise ValueError("Both creation anchor fields are required.")

    query = select(Incident)
    if search:
        escaped = _escape_like(search.strip())
        if escaped:
            query = query.where(
                or_(
                    Incident.title.ilike(f"%{escaped}%", escape="\\"),
                    func.lower(cast(Incident.id, String)).like(f"{escaped.lower()}%", escape="\\"),
                )
            )
    if filters:
        if filters.get("workload_id") is not None:
            identity(db, filters["workload_id"])
            query = query.where(Incident.workload_id == filters["workload_id"])
        if filters.get("origin"):
            query = query.where(Incident.origin_kind == filters["origin"])
        if filters.get("state"):
            query = query.where(Incident.state == filters["state"])
        if filters.get("severity"):
            query = query.where(Incident.severity == filters["severity"])
        if filters.get("alert_signal"):
            query = query.where(Incident.alert_signal == filters["alert_signal"])
    if not filters or filters.get("state") is None:
        if view is None:
            query = query.where(Incident.state.in_(("open", "acknowledged")))

    ranked_runs = select(
        InvestigationRun.id.label("id"),
        InvestigationRun.incident_id.label("incident_id"),
        InvestigationRun.state.label("state"),
        func.row_number()
        .over(
            partition_by=InvestigationRun.incident_id,
            order_by=(InvestigationRun.created_at.desc(), InvestigationRun.id.desc()),
        )
        .label("position"),
    ).subquery()
    if run_state:
        query = query.where(
            exists(
                select(1)
                .select_from(ranked_runs)
                .where(
                    ranked_runs.c.incident_id == Incident.id,
                    ranked_runs.c.position == 1,
                    ranked_runs.c.state == run_state,
                )
            )
        )

    execution_class_expr = classification_expression(
        Execution.state, Execution.holds_lock, Execution.maintenance_released
    )

    def has_execution(*conditions):
        return exists(
            select(Execution.id)
            .join(ActionPlan, ActionPlan.id == Execution.plan_id)
            .where(ActionPlan.incident_id == Incident.id, *conditions)
        )

    def has_run(*conditions):
        return exists(
            select(InvestigationRun.id).where(
                InvestigationRun.incident_id == Incident.id, *conditions
            )
        )

    if execution_class:
        class_condition = (
            execution_class_expr.in_(("terminal_unknown_locked", "terminal_unknown_released"))
            if execution_class == "terminal_unknown"
            else execution_class_expr == execution_class
        )
        query = query.where(has_execution(class_condition))

    capacity_or_blocked_run = has_run(
        or_(
            InvestigationRun.state.in_(("failed", "insufficient_evidence", "waiting_for_quota")),
            and_(
                InvestigationRun.state == "queued",
                InvestigationRun.queue_reason == "capacity",
            ),
        )
    )
    uncertain_or_active_execution = has_execution(
        or_(
            execution_class_expr.in_(
                ("active", "terminal_unknown_locked", "terminal_unknown_released", "unrecognized")
            ),
            Execution.state == "manual_intervention",
        )
    )
    blocked = and_(
        Incident.state != "resolved",
        or_(capacity_or_blocked_run, uncertain_or_active_execution),
    )
    if view == "blocked":
        query = query.where(blocked)
    elif view == "resolved":
        query = query.where(Incident.state == "resolved")
    elif view == "awaiting_decision":
        query = query.where(
            exists(
                select(ActionPlan.id).where(
                    ActionPlan.incident_id == Incident.id,
                    ActionPlan.state == "proposed",
                    ActionPlan.expires_at > func.now(),
                )
            )
        )
    elif view == "verifying":
        query = query.where(
            has_execution(Execution.state == "verifying", execution_class_expr == "active")
        )
    elif view == "needs_attention":
        query = query.where(
            Incident.state != "resolved",
            or_(Incident.alert_signal == "firing", blocked),
        )

    if anchor_created_at is None:
        newest = db.execute(
            select(Incident.created_at, Incident.id)
            .order_by(Incident.created_at.desc(), Incident.id.desc())
            .limit(1)
        ).first()
        if newest is None:
            anchor_created_at = database_now(db)
            anchor_id = UUID(int=(1 << 128) - 1)
        else:
            anchor_created_at, anchor_id = newest
    query = query.where(
        or_(
            Incident.created_at < anchor_created_at,
            and_(Incident.created_at == anchor_created_at, Incident.id <= anchor_id),
        )
    )

    sort_column = SORT_COLUMNS[sort_by]
    ordered = sort_column.desc() if sort_order == "desc" else sort_column.asc()
    if sort_by in NULLABLE_SORT_COLUMNS:
        ordered = ordered.nulls_last()
    rows = db.scalars(
        query.order_by(ordered, Incident.id.asc()).limit(limit + 1).offset(offset)
    ).all()
    incidents = rows[:limit]
    incident_ids = [item.id for item in incidents]
    latest_runs: dict[UUID, WorkQueueRun] = {}
    run_rows = (
        db.execute(
            select(InvestigationRun, User.username)
            .join(ranked_runs, InvestigationRun.id == ranked_runs.c.id)
            .outerjoin(User, User.id == InvestigationRun.requested_by)
            .where(ranked_runs.c.position == 1, ranked_runs.c.incident_id.in_(incident_ids))
        ).all()
        if incident_ids
        else []
    )
    for run, requester_name in run_rows:
        latest_runs[run.incident_id] = WorkQueueRun(
            id=run.id,
            state=run.state,
            origin_kind=run.origin_kind,
            queue_reason=run.queue_reason,
            reason=run.reason,
            requested_by=run.requested_by,
            requested_by_name=requester_name,
            created_at=run.created_at,
            outcome_code=run.outcome_code,
        )

    run_blocker_rows = (
        db.execute(
            select(
                InvestigationRun.incident_id,
                InvestigationRun.state,
                InvestigationRun.queue_reason,
            ).where(
                InvestigationRun.incident_id.in_(incident_ids),
                or_(
                    InvestigationRun.state.in_(
                        ("failed", "insufficient_evidence", "waiting_for_quota")
                    ),
                    and_(
                        InvestigationRun.state == "queued",
                        InvestigationRun.queue_reason == "capacity",
                    ),
                ),
            )
        ).all()
        if incident_ids
        else []
    )
    run_blockers: dict[UUID, set[str]] = {}
    for run_incident_id, run_status, _ in run_blocker_rows:
        run_blockers.setdefault(run_incident_id, set()).add(run_status)

    acknowledgements = {}
    activity_rows = (
        db.execute(
            select(
                IncidentActivity.incident_id, IncidentActivity.actor_id, IncidentActivity.created_at
            ).where(
                IncidentActivity.id.in_(
                    select(IncidentActivity.id)
                    .where(
                        IncidentActivity.incident_id.in_(incident_ids),
                        IncidentActivity.kind == "acknowledge",
                    )
                    .distinct(IncidentActivity.incident_id)
                    .order_by(
                        IncidentActivity.incident_id,
                        IncidentActivity.created_at.desc(),
                        IncidentActivity.id.desc(),
                    )
                )
            )
        ).all()
        if incident_ids
        else []
    )
    for activity in activity_rows:
        acknowledgements.setdefault(activity.incident_id, activity)

    execution_rows = (
        db.execute(
            select(Execution, ActionPlan.incident_id)
            .join(ActionPlan, ActionPlan.id == Execution.plan_id)
            .where(
                ActionPlan.incident_id.in_(incident_ids),
                or_(execution_class_expr != "terminal", Execution.state == "manual_intervention"),
            )
            .order_by(ActionPlan.incident_id, Execution.created_at.desc(), Execution.id.desc())
        ).all()
        if incident_ids
        else []
    )
    executions: dict[UUID, list[WorkQueueExecution]] = {}
    for execution, owner_incident_id in execution_rows:
        execution_classification = classify_execution(
            execution.state, execution.holds_lock, execution.maintenance_released
        )
        if execution_classification == "terminal" and execution.state != "manual_intervention":
            continue
        executions.setdefault(owner_incident_id, []).append(
            WorkQueueExecution(
                id=execution.id,
                state=execution.state,
                classification=execution_classification,
                holds_lock=execution.holds_lock,
                workload_id=execution.workload_id,
                created_at=execution.created_at,
            )
        )

    actor_ids = {item.created_by for item in incidents if item.created_by}
    actor_ids.update(run.requested_by for run, _ in run_rows if run.requested_by)
    actor_ids.update(item.actor_id for item in acknowledgements.values())
    actor_names = (
        dict(db.execute(select(User.id, User.username).where(User.id.in_(actor_ids))).all())
        if actor_ids
        else {}
    )
    page_items = []
    for incident in incidents:
        ack = acknowledgements.get(incident.id)
        page_items.append(
            WorkQueueItem(
                **IncidentView.model_validate(incident).model_dump(),
                created_by_name=actor_names.get(incident.created_by),
                acknowledged_by=ack.actor_id if ack else None,
                acknowledged_by_name=actor_names.get(ack.actor_id) if ack else None,
                acknowledged_at=ack.created_at if ack else None,
                latest_run=latest_runs.get(incident.id),
                run_blockers=sorted(run_blockers.get(incident.id, set())),
                relevant_executions=executions.get(incident.id, []),
            )
        )
    return IncidentPage(
        items=page_items,
        next_offset=offset + limit if len(rows) > limit else None,
        creation_anchor=CreationAnchor(created_at=anchor_created_at, id=anchor_id),
    )
