"""Recovery-owned execution and target-lock classification.

Admission, promotion, claim, resolve and queue consumers share these decisions rather than
maintaining their own lists. Unknown or contradictory persisted states fail closed.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.recovery.models import ActionPlan, Execution

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


def classify(state: str, holds_lock: bool, maintenance_released: bool) -> ExecutionClass:
    if state in ACTIVE:
        return "active"
    if state == "outcome_unknown":
        if holds_lock:
            return "terminal_unknown_locked"
        return "terminal_unknown_released" if maintenance_released else "unrecognized"
    if state in TERMINAL and not holds_lock:
        return "terminal"
    return "unrecognized"


def blocking_execution(db: Session, incident_id: UUID) -> UUID | None:
    """Inspect every execution of the incident, not only the newest result."""
    rows = db.execute(
        select(Execution.id, Execution.state, Execution.holds_lock, Execution.maintenance_released)
        .join(ActionPlan, ActionPlan.id == Execution.plan_id)
        .where(ActionPlan.incident_id == incident_id)
    ).all()
    for row in rows:
        if classify(row.state, row.holds_lock, row.maintenance_released) in BLOCKING:
            return row.id
    return None
