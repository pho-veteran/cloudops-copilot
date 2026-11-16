"""Recovery-owned execution and target-lock classification.

Admission, promotion, claim, resolve and queue consumers share these decisions rather than
maintaining their own lists. Unknown or contradictory persisted states fail closed.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import case, false, func, not_, select
from sqlalchemy.orm import Session

from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.shared.jobs import Job

ExecutionClass = Literal[
    "active",
    "terminal_unknown_locked",
    "terminal_unknown_released",
    "terminal",
    "unrecognized",
]

# Current producers emit queued/executing; the others are declared E07 states.
ACTIVE = frozenset({"queued", "executing", "verifying", "reconciling", "rolling_back"})
TERMINAL = frozenset({"manual_intervention", "succeeded", "failed", "no_op", "rolled_back"})
BLOCKING = frozenset({"active", "unrecognized"})


def classify(
    state: str, holds_lock: bool, maintenance_released: bool, result=None
) -> ExecutionClass:
    if state in ACTIVE:
        return "active"
    if state == "outcome_unknown":
        if holds_lock:
            return "unrecognized" if maintenance_released else "terminal_unknown_locked"
        return "terminal_unknown_released" if maintenance_released else "unrecognized"
    if state in TERMINAL and not holds_lock and not maintenance_released:
        return "terminal"
    if state == "manual_intervention" and not holds_lock and acknowledged_release(result):
        return "terminal"
    return "unrecognized"


def classification_expression(state, holds_lock, maintenance_released, result=None):
    """SQL equivalent of :func:`classify` for read-only server-side queue filters.

    The state sets above remain the single source of truth for both Python consumers and
    SQL-backed projections. Keep branch order aligned with ``classify``.
    """
    acknowledged = (
        (state == "manual_intervention")
        & (
            func.coalesce(result["attribution"].as_string(), result["outcome"].as_string())
            == "acknowledged"
        )
        & result["maintenance_request_id"]
        .as_string()
        .op("~")("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        if result is not None
        else false()
    )
    return case(
        (state.in_(ACTIVE), "active"),
        (
            state == "outcome_unknown",
            case(
                (holds_lock.is_(True) & maintenance_released.is_(False), "terminal_unknown_locked"),
                (holds_lock.is_(True), "unrecognized"),
                (maintenance_released.is_(True), "terminal_unknown_released"),
                else_="unrecognized",
            ),
        ),
        (
            state.in_(TERMINAL)
            & not_(holds_lock.is_(True))
            & (maintenance_released.is_(False) | acknowledged),
            "terminal",
        ),
        else_="unrecognized",
    )


def _maintenance_proof(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    value = result.get("maintenance_request_id")
    if not isinstance(value, str):
        return False
    try:
        if str(UUID(value)) != value:
            return False
    except ValueError:
        return False
    return True


def acknowledged_release(result):
    return (
        isinstance(result, dict)
        and result.get("attribution", result.get("outcome")) == "acknowledged"
        and _maintenance_proof(result)
    )


def terminal_unknown_eligible(
    state: str,
    holds_lock: bool,
    maintenance_released: bool,
    job_state: str | None,
    result: dict | None,
) -> bool:
    """Only a final producer receipt, or recorded maintenance proof, permits handoff."""
    return (
        classify(state, holds_lock, maintenance_released, result)
        in {"terminal_unknown_locked", "terminal_unknown_released"}
        and (
            job_state in {"completed", "failed"}
            or (maintenance_released and _maintenance_proof(result))
        )
        and isinstance(result, dict)
        and result.get("outcome") == "outcome_unknown"
        and (not maintenance_released or _maintenance_proof(result))
    )


def blocking_execution(db: Session, incident_id: UUID) -> UUID | None:
    """Inspect every execution of the incident, not only the newest result."""
    rows = db.execute(
        select(
            Execution.id,
            Execution.state,
            Execution.holds_lock,
            Execution.maintenance_released,
            Execution.result,
            Job.state.label("job_state"),
        )
        .join(ActionPlan, ActionPlan.id == Execution.plan_id)
        .outerjoin(Job, (Job.kind == "recovery_execution_v1") & (Job.subject_id == Execution.id))
        .where(ActionPlan.incident_id == incident_id)
    ).all()
    for row in rows:
        if classify(row.state, row.holds_lock, row.maintenance_released, row.result) in BLOCKING:
            return row.id
        if row.maintenance_released and not row.holds_lock and acknowledged_release(row.result):
            continue
        if row.state in TERMINAL and row.job_state in {"queued", "running"}:
            return row.id
        if row.state == "outcome_unknown" and not terminal_unknown_eligible(
            row.state, row.holds_lock, row.maintenance_released, row.job_state, row.result
        ):
            return row.id
    return None
