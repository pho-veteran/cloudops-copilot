"""Bounded, fenced receipt/observation reads. This worker never dispatches a mutation."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from cloudops.features.investigations.collector import Interrupted
from cloudops.features.recovery import service
from cloudops.features.recovery.fields import selected
from cloudops.features.recovery.models import Execution
from cloudops.features.recovery.outcomes import RECONCILE_ATTEMPTS, RECONCILE_INTERVAL_SECONDS
from cloudops.features.workloads.kubernetes import Kubernetes, snapshot
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import JobAttempt, _fenced, defer_owned, start_call


def exhausted(db, row):
    row.state, row.holds_lock = "outcome_unknown", True
    row.finished_at = database_now(db)
    service.record_outcome(
        row,
        {
            **row.result,
            "outcome": "outcome_unknown",
            "attribution": "outcome_unknown",
            "verification": "inconclusive",
            "reason": "Read-only reconciliation budget exhausted. Keep target quarantine; investigate or hand off the unknown result and use trusted maintenance for future authority.",
        },
    )


def eligible(db, row):
    return (
        not row.maintenance_released
        and row.state == "reconciling"
        and row.reconcile_deadline is not None
        and row.reconcile_deadline > database_now(db)
        and row.reconcile_attempts < RECONCILE_ATTEMPTS
    )


def done(db, job):
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.lease_token == job.lease_token))
    attempt.state, attempt.finished_at = "succeeded", database_now(db)
    job.state, job.error, job.lease_token, job.lease_until = "completed", None, None, None


class ReconciliationHandler:
    def eligible(self, db, subject_id):
        row = db.get(Execution, subject_id, with_for_update=True)
        if row is None or row.maintenance_released or row.state != "reconciling":
            return False
        if not eligible(db, row):
            exhausted(db, row)
            return False
        return True

    def started(self, db, subject_id):
        pass

    def failed(self, db, subject_id, reason, terminal):
        row = db.get(Execution, subject_id, with_for_update=True)
        if row is None or row.maintenance_released or row.state != "reconciling":
            return
        if terminal or not eligible(db, row):
            exhausted(db, row)

    def publish(self, db, subject_id, result):
        raise RuntimeError("Receipt polls must use their fenced deferral runner.")


def reconcile(claim, gate, lease_lost):
    with gate.admitted() as admission, sessions()() as db, db.begin():
        pair = _fenced(db, claim, admission)
        if pair is None:
            raise Interrupted()
        job, _ = pair
        row = db.get(Execution, claim.subject_id, with_for_update=True)
        if row.maintenance_released or row.state != "reconciling":
            done(db, job)
            return
        if not eligible(db, row):
            exhausted(db, row)
            done(db, job)
            return
        row.reconcile_attempts += 1
        request = row.dispatch_request
        deadline = row.reconcile_deadline
        workload = db.get(Workload, row.workload_id)
    if lease_lost.is_set() or not start_call(claim, gate):
        raise Interrupted()
    result = service.lookup(claim.subject_id, request)
    if lease_lost.is_set() or not start_call(claim, gate):
        raise Interrupted()
    observation = {"state": "unavailable"}
    try:
        if datetime.now(timezone.utc) >= deadline:
            raise ValueError("Observation deadline elapsed.")
        current = Kubernetes().deployment(workload)
        if request and request.get("schema_version") == "RecoveryDispatch/v2":
            actual = {
                "uid": current["metadata"]["uid"],
                "replicas": current["spec"].get("replicas", 1),
                "containers": selected(current)[0],
            }
            matches = actual == request["desired"]
        elif request:
            desired = {**request["before"], "replicas": request["replicas"]}
            matches = snapshot(current) == desired
        else:
            matches = None
        observation = {
            "state": "observed",
            "uid": current["metadata"]["uid"],
            "resource_version": current["metadata"]["resourceVersion"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "desired_fields_match": matches,
        }
    except (DomainError, ValueError, TypeError, KeyError):
        pass
    with gate.admitted() as admission, sessions()() as db, db.begin():
        pair = _fenced(db, claim, admission)
        if pair is None:
            raise Interrupted()
        job, _ = pair
        row = db.get(Execution, claim.subject_id, with_for_update=True)
        if row.maintenance_released or row.state != "reconciling":
            done(db, job)
            return
        service.record_outcome(row, {**result, "reconciliation_observation": observation})
        if result["outcome"] != "outcome_unknown":
            service.ExecutionHandler().publish(db, row.id, row.result)
            done(db, job)
        elif not eligible(db, row):
            exhausted(db, row)
            done(db, job)
        else:
            due = min(
                row.reconcile_deadline,
                database_now(db) + timedelta(seconds=RECONCILE_INTERVAL_SECONDS),
            )
            defer_owned(db, claim, due)
