"""Retire restored application authority; external journals and target quarantine remain intact."""

import json
import os
import socket
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from cloudops.features.access.models import LoginSession
from cloudops.features.recovery.classification import ACTIVE
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.recovery.outcomes import RECONCILE_JOB
from cloudops.shared.database import database_now
from cloudops.shared.jobs import Job, JobAttempt


def closed_gateway_targets():
    path = Path(os.environ.get("EXECUTOR_ROOT", "/var/lib/cloudops-executor")) / "maintenance.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(str(path))
            connection.sendall(
                json.dumps({"request_id": str(uuid4()), "operation": "target_status"}).encode()
                + b"\n"
            )
            value = json.loads(connection.makefile("rb").readline(128001))
        targets = value["targets"]
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 100
            or len({item["uid"] for item in targets}) != len(targets)
            or any(
                item["enabled"] is not False or item["recovery_mode"] is not True
                for item in targets
            )
        ):
            raise ValueError
        return targets
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError(
            "Keep every configured gateway target closed in recovery before restored-work cleanup."
        ) from None


def retire(db):
    now, identity = database_now(db), str(uuid4())
    sessions = db.execute(delete(LoginSession)).rowcount
    reviews = db.execute(delete(PlanReview)).rowcount
    jobs = db.scalars(
        select(Job)
        .where(
            Job.kind.in_(["recovery_execution_v1", RECONCILE_JOB]),
            Job.state.in_(["queued", "held", "running"]),
        )
        .with_for_update()
    ).all()
    active_ids = {job.subject_id for job in jobs}
    for job in jobs:
        if job.lease_token:
            attempt = db.scalar(select(JobAttempt).where(JobAttempt.lease_token == job.lease_token))
            if attempt:
                attempt.state, attempt.finished_at, attempt.error = (
                    "failed",
                    now,
                    "Restored authority retired.",
                )
        job.state, job.error = (
            "failed",
            "Restored authority retired; no dispatch replay is permitted.",
        )
        job.lease_token, job.lease_until = None, None
    rows = db.scalars(
        select(Execution)
        .where(Execution.state.in_(ACTIVE) | Execution.id.in_(active_ids))
        .with_for_update()
    ).all()
    for row in rows:
        known = row.result.get("attribution", row.result.get("outcome")) == "acknowledged"
        value = "acknowledged" if known else "outcome_unknown"
        row.state, row.holds_lock = (
            ("manual_intervention", False) if known else ("outcome_unknown", True)
        )
        row.maintenance_released = False
        row.dispatch_started_at = row.dispatch_started_at or now
        row.reconcile_deadline = row.reconcile_deadline or now
        row.finished_at = now
        row.result = {
            **row.result,
            "outcome": value,
            "attribution": value,
            "verification": row.result.get("verification")
            if row.result.get("verification") in {"passed", "failed"}
            else "inconclusive",
            "restore_cleanup_id": identity,
            "reason": row.result.get("reason")
            or "Restored execution authority was retired; write outcome remains unknown.",
            "restore_reason": "Restored sessions, approvals and jobs retired. Snapshot absence of call admission cannot establish no write; external authority remains quarantined until root proof.",
        }
    plans = db.scalars(
        select(ActionPlan)
        .where(
            (ActionPlan.state == "proposed")
            | ((ActionPlan.state == "approved") & ActionPlan.id.in_([row.plan_id for row in rows]))
        )
        .with_for_update()
    ).all()
    for plan in plans:
        plan.state = "invalidated"
    return {
        "cleanup_id": identity,
        "sessions_revoked": sessions,
        "reviews_retired": reviews,
        "jobs_retired": len(jobs),
        "executions_retired": len(rows),
        "plans_invalidated": len(plans),
    }
