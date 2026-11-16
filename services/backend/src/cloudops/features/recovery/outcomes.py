"""One truthful projection for current execution readers and legacy retained receipts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

RECONCILE_SECONDS = 120
RECONCILE_ATTEMPTS = 6
RECONCILE_INTERVAL_SECONDS = 10
RECONCILE_JOB = "recovery_reconciliation_v1"

Attribution = Literal["not_sent", "acknowledged", "denied", "outcome_unknown"]
Verification = Literal["not_performed", "pending", "passed", "failed", "inconclusive"]


class Reconciliation(BaseModel):
    attempts: int
    maximum_attempts: int = RECONCILE_ATTEMPTS
    deadline: datetime | None


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["RecoveryOutcome/v1"] = "RecoveryOutcome/v1"
    operation_id: UUID
    attribution: Attribution
    verification: Verification
    future_authority: Literal[
        "target_locked", "maintenance_proof_recorded_recheck_required", "fresh_preflight_required"
    ]
    reason: str
    reconciliation: Reconciliation | None = None


def attribution(row):
    result = row.result or {}
    value = result.get("attribution", result.get("outcome"))
    if result.get("code") == "no_op":
        return "not_sent"
    if value in {"not_sent", "acknowledged", "denied", "outcome_unknown"}:
        return value
    if row.state == "queued" and row.dispatch_started_at is None:
        return "not_sent"
    return "outcome_unknown"


def project(row):
    result = row.result or {}
    verification = result.get("verification", "not_performed")
    if verification not in {"not_performed", "pending", "passed", "failed", "inconclusive"}:
        verification = "inconclusive"
    return Outcome(
        operation_id=row.id,
        attribution=attribution(row),
        verification=verification,
        future_authority="target_locked"
        if row.holds_lock
        else "maintenance_proof_recorded_recheck_required"
        if row.maintenance_released
        else "fresh_preflight_required",
        reason=str(result.get("reason") or "No final execution receipt is available.")[:2000],
        reconciliation=Reconciliation(
            attempts=row.reconcile_attempts or 0, deadline=row.reconcile_deadline
        )
        if row.reconcile_deadline is not None
        else None,
    )
