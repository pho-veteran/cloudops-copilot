"""Durable admission and passive evidence assessments for explicit checks."""

import asyncio
from datetime import timedelta
from time import monotonic

from sqlalchemy import and_, or_, select, text

from cloudops.features.connections import sources
from cloudops.features.connections.models import ConnectionCheck
from cloudops.features.connections.schemas import (
    CheckStatus,
    Component,
    MetricsCheck,
    Reason,
    ReceiptView,
    Source,
    Status,
)
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now
from cloudops.shared.errors import DomainError

DEADLINE_SECONDS = 8
COOLDOWN = timedelta(seconds=60)
CHECK_FRESHNESS_SECONDS = 300
ADMISSION_LOCK = 742105
COMPONENTS: dict[Component, Source] = {
    "cluster": "live_kubernetes",
    "prometheus": "prometheus",
    "database": "runtime",
    "worker": "runtime",
}
REASONS: dict[Reason, tuple[Status, str]] = {
    "database_read": ("available", "Application database query succeeded."),
    "heartbeat_fresh": ("available", "Worker heartbeat is fresh."),
    "heartbeat_missing": (
        "missing",
        "No worker heartbeat was recorded. Start the configured worker.",
    ),
    "heartbeat_stale": ("stale", "The worker heartbeat is stale. Inspect the configured worker."),
    "deployment_read": (
        "available",
        "Read the exact configured Deployment. Inspect Workloads for rollout status.",
    ),
    "query_succeeded": ("available", "Prometheus returned scoped data for the current window."),
    "metrics_missing": (
        "missing",
        "Prometheus returned no usable samples for this workload. Inspect its scrape and labels.",
    ),
    "metrics_partial": (
        "partial",
        "Prometheus returned incomplete coverage for this workload. Inspect its scrape and labels.",
    ),
    "metrics_stale": (
        "stale",
        "Scoped query points end before the current window. Inspect scrape progress.",
    ),
    "scope_missing": (
        "unconfigured",
        "Configure and synchronize an active live workload before checking this source.",
    ),
    "scope_changed": (
        "unconfigured",
        "The configured workload scope changed. Synchronize the deployment scope.",
    ),
    "target_changed": (
        "unavailable",
        "The configured Deployment is missing or its identity changed. Synchronize the workload scope.",
    ),
    "config_disabled": (
        "unconfigured",
        "Prometheus collection is disabled in the deployment configuration.",
    ),
    "config_missing": (
        "unconfigured",
        "The configured read credentials or CA file are missing. Inspect the deployment configuration.",
    ),
    "config_invalid": (
        "unconfigured",
        "The read configuration is invalid. Check the configured HTTPS endpoint, CA and credentials.",
    ),
    "config_changed": (
        "unavailable",
        "The configured source changed during the check. Run a new check for the current binding.",
    ),
    "permission_denied": (
        "denied",
        "The source refused the configured credentials. Check the scoped read permissions.",
    ),
    "redirect_refused": (
        "unavailable",
        "The source returned a redirect. Check the configured HTTPS endpoint.",
    ),
    "timeout": (
        "unavailable",
        "The source check reached its deadline. Check connectivity before trying again.",
    ),
    "transport_unavailable": (
        "unavailable",
        "The configured source could not be reached or returned an error. Check its service and TLS configuration.",
    ),
    "invalid_response": (
        "unavailable",
        "The source returned an unexpected response. Check the configured API and data format.",
    ),
    "response_limit": (
        "unavailable",
        "The source response exceeded the limits for a connection check.",
    ),
    "check_pending": ("pending", "A connection check is in progress."),
    "never_observed": ("never_observed", "No check has been recorded for this source."),
    "binding_changed": (
        "never_observed",
        "The current workload or configuration needs a new check. Previous results are historical.",
    ),
    "evidence_stale": (
        "stale",
        "The latest successful check is past its freshness threshold. Run a new check.",
    ),
    "invalid_timestamp": (
        "unavailable",
        "The observation timestamp is ahead of the database clock. Check service clocks.",
    ),
}


def latest_attempt(component):
    return (
        select(ConnectionCheck)
        .where(ConnectionCheck.component == component, ConnectionCheck.attempted_at.is_not(None))
        .order_by(ConnectionCheck.attempted_at.desc(), ConnectionCheck.id.desc())
        .limit(1)
    )


def lock_admission(db):
    # Admission and publication share a short transaction lock, including cross-component key conflicts.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK})


def finish(row, result, finished_at, *, evidence_at=None):
    status, row.reason = REASONS[result.reason]
    if result.reason == "heartbeat_missing":
        status = "stale"  # Preserve the original worker receipt status for existing clients.
    row.status = status if status in ("available", "partial", "stale") else "unavailable"
    row.reason_code = result.reason
    row.finished_at = finished_at
    # A timeout has no source observation. Keep its history ordering at admission.
    if result.reason != "timeout":
        row.observed_at = finished_at
    row.evidence_at = evidence_at
    if result.coverage:
        row.metrics = MetricsCheck(
            query_succeeded_at=finished_at,
            coverage=result.coverage,
            coverage_reasons=list(result.coverage_reasons),
            latest_evaluation_at=result.evaluation_at,
        ).model_dump(mode="json")


def expired(row, now):
    return row.deadline_at is not None and row.finished_at is None and row.deadline_at <= now


def current_binding(row, selection):
    return selection.key is not None and row.binding_key == selection.key


def view(row, selection, now):
    receipt = ReceiptView.model_validate(row)
    change = {"historical": not current_binding(row, selection)}
    if expired(row, now):
        change.update(
            reason_code="timeout",
            reason=REASONS["timeout"][1],
            finished_at=row.deadline_at,
        )
    return receipt.model_copy(update=change)


def runtime_result(db, component, now):
    if component == "database":
        return sources.Result("database_read"), now
    beat = db.get(WorkerHeartbeat, "collector")
    if not beat:
        return sources.Result("heartbeat_missing"), None
    age = (now - beat.observed_at).total_seconds()
    reason = (
        "invalid_timestamp"
        if age < 0
        else ("heartbeat_fresh" if age <= settings().worker_stale_seconds else "heartbeat_stale")
    )
    return sources.Result(reason), None if age < 0 else beat.observed_at


def create(db, actor_id, component: Component, key):
    with db.begin():
        lock_admission(db)
        if key is not None:
            original = db.scalar(
                select(ConnectionCheck).where(ConnectionCheck.idempotency_key == key)
            )
            if original:
                if original.created_by != actor_id or original.component != component:
                    raise DomainError(
                        409, "idempotency_conflict", "The key belongs to a different check intent."
                    )
                selection = sources.selected(db, component)
                now = database_now(db)
                if expired(original, now):
                    finish(original, sources.Result("timeout"), original.deadline_at)
                return view(original, selection, now)

        recent = db.scalar(latest_attempt(component))
        now = database_now(db)
        if recent and recent.finished_at is None and recent.deadline_at > now:
            raise DomainError(409, "check_in_progress", "Wait for the check already in progress.")
        if recent and recent.attempted_at + COOLDOWN > now:
            raise DomainError(
                409, "check_cooldown", "Wait 60 seconds between checks for this source."
            )
        if recent and expired(recent, now):
            finish(recent, sources.Result("timeout"), recent.deadline_at)

        selection = sources.selected(db, component, fence=True)
        cutoff = monotonic() + DEADLINE_SECONDS
        now = database_now(db)
        row = ConnectionCheck(
            component=component,
            status="unavailable",
            reason=REASONS["check_pending"][1],
            reason_code="check_pending",
            source=COMPONENTS[component],
            created_by=actor_id,
            idempotency_key=key,
            attempted_at=now,
            deadline_at=now + timedelta(seconds=DEADLINE_SECONDS),
            observed_at=now,
            binding_key=selection.key,
            config_id=selection.config_id,
            target=selection.target.model_dump(mode="json") if selection.target else None,
        )
        db.add(row)
        if selection.issue:
            finish(row, sources.Result(selection.issue), database_now(db))
        elif component in ("worker", "database"):
            result, evidence_at = runtime_result(db, component, now)
            finish(row, result, database_now(db), evidence_at=evidence_at)
        db.flush()
        if row.finished_at:
            return view(row, selection, row.finished_at)

    # The attempt is committed and the database connection is released before contacting the source.
    attempt_id, started_at = row.id, row.attempted_at
    result = (
        asyncio.run(sources.read(selection, started_at, cutoff))
        if monotonic() < cutoff
        else sources.Result("timeout")
    )
    deadline_reached = monotonic() >= cutoff

    with db.begin():
        completed_at = database_now(db)
        lock_admission(db)
        row = db.get(ConnectionCheck, attempt_id, populate_existing=True)
        current = sources.selected(db, component, fence=True)
        assessed_at = database_now(db)
        if row.finished_at is None:
            if deadline_reached or completed_at >= row.deadline_at or result.reason == "timeout":
                finish(row, sources.Result("timeout"), min(completed_at, row.deadline_at))
            elif not current_binding(row, current):
                target_changed = current.target != selection.target
                finish(
                    row,
                    sources.Result("target_changed" if target_changed else "config_changed"),
                    completed_at,
                )
            else:
                evidence_at = (
                    completed_at if result.reason == "deployment_read" or result.coverage else None
                )
                finish(row, result, completed_at, evidence_at=evidence_at)
        return view(row, current, assessed_at)


def history(db):
    selections = {component: sources.selected(db, component) for component in COMPONENTS}
    now = database_now(db)
    rows = db.scalars(
        select(ConnectionCheck)
        .order_by(ConnectionCheck.observed_at.desc(), ConnectionCheck.id.desc())
        .limit(50)
    )
    return [view(row, selections[row.component], now) for row in rows]


def status_for(db, component, selection, now):
    threshold = {
        "worker": settings().worker_stale_seconds,
        "cluster": CHECK_FRESHNESS_SECONDS,
        "prometheus": CHECK_FRESHNESS_SECONDS,
    }.get(component)
    latest = db.scalar(latest_attempt(component))
    receipt = view(latest, selection, now) if latest else None
    binding_matches = latest and current_binding(latest, selection)
    last_success = None
    last_failure = None
    if selection.key:
        attempts = latest_attempt(component).where(ConnectionCheck.binding_key == selection.key)
        success = db.scalar(attempts.where(ConnectionCheck.status == "available"))
        last_success = (
            success.evidence_at
            if success and success.evidence_at and success.evidence_at <= now
            else None
        )
        # Distinct status predicates keep both lookups bounded by the binding index.
        failures = [
            db.scalar(
                attempts.where(
                    ConnectionCheck.status == outcome,
                    or_(
                        ConnectionCheck.finished_at <= now,
                        and_(
                            ConnectionCheck.finished_at.is_(None),
                            ConnectionCheck.deadline_at <= now,
                        ),
                    ),
                )
            )
            for outcome in ("unavailable", "stale", "partial")
        ]
        last_failure = max(
            (
                row.finished_at or row.deadline_at
                for row in failures
                if row and (row.finished_at or expired(row, now))
            ),
            default=None,
        )

    reason = selection.issue
    if reason is None:
        if latest is None:
            reason = "never_observed"
        elif not binding_matches:
            reason = "binding_changed"
        else:
            reason = receipt.reason_code
            if any(
                value and value > now
                for value in (receipt.attempted_at, receipt.finished_at, receipt.evidence_at)
            ):
                reason = "invalid_timestamp"
            elif (
                receipt.status == "available"
                and receipt.evidence_at
                and threshold is not None
                and (now - receipt.evidence_at).total_seconds() > threshold
            ):
                reason = "heartbeat_stale" if component == "worker" else "evidence_stale"
    status, message = REASONS[reason]
    return CheckStatus(
        component=component,
        source=COMPONENTS[component],
        status=status,
        reason_code=reason,
        reason=message,
        assessed_at=now,
        target=selection.target,
        config_id=selection.config_id,
        observed_at=receipt.evidence_at
        if binding_matches and reason != "invalid_timestamp"
        else None,
        last_attempt_at=receipt.attempted_at if receipt else None,
        last_success_at=last_success,
        last_failure_at=last_failure,
        stale_after_seconds=threshold,
        latest_check_id=receipt.id if receipt else None,
        metrics=receipt.metrics if binding_matches else None,
    )


def statuses(db):
    selections = {component: sources.selected(db, component) for component in COMPONENTS}
    now = database_now(db)
    return [
        status_for(db, component, selection, now) for component, selection in selections.items()
    ]
