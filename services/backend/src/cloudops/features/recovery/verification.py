"""Persisted, fenced verification with one reservation per fixed observation slot."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from cloudops.features.investigations.collector import Interrupted
from cloudops.features.recovery import verification_sources as sources
from cloudops.features.recovery.models import ActionPlan, Execution, Verification
from cloudops.features.recovery.verification_evaluator import (
    evaluate,
    metric_summary,
    offered_baseline,
)
from cloudops.features.recovery.verification_policy import (
    VERIFICATION_JOB,
    VerificationProfile,
    require_plan,
)
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, _fenced, defer_owned, start_call

MAX_CAPTURE_BYTES = 262144


def denied(reason, *, no_op=False):
    return {
        "outcome": "denied",
        "attribution": "not_sent",
        "verification": "not_performed",
        "reason": reason,
        **({"code": "no_op"} if no_op else {}),
    }


def bounded(value):
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_CAPTURE_BYTES:
        return {"error": "recovery_observation_payload_limit"}
    return value


def prepare(execution_id, *, before_call):
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id, with_for_update=True)
        plan = db.get(ActionPlan, row.plan_id)
        if plan.content.get("schema_version") != "RecoveryPlan/v2":
            return None
        try:
            profile = require_plan(plan.content)
        except DomainError as error:
            return denied(error.message)
        prior = db.get(Verification, row.id)
        if prior is not None:
            if prior.result.get("outcome"):
                return prior.result
            if prior.baseline and database_now(db) - datetime.fromisoformat(
                prior.baseline["window_end"]
            ) > timedelta(seconds=60):
                return denied("The original pre-change baseline expired; prepare a new plan.")
            # A lost baseline reservation is never retried against a different pre-change window.
            return (
                None
                if prior.baseline
                else denied("The original baseline read was interrupted; prepare a new plan.")
            )
        if row.maintenance_released or row.dispatch_started_at is not None:
            return denied("The original pre-change observation window is unavailable.")
        workload = db.get(Workload, row.workload_id)
        incident_id = plan.incident_id
        end = database_now(db)
        start = end - timedelta(seconds=60)
        db.add(
            Verification(
                execution_id=row.id,
                profile=profile.model_dump(mode="json"),
                profile_digest=profile.digest(),
                samples={},
                result={},
            )
        )
    before_call()
    baseline = bounded(
        sources.capture(
            execution_id,
            incident_id,
            workload,
            start,
            end,
            before_call=before_call,
            profile=profile,
        )
    )
    baseline = {**baseline, "window_start": start.isoformat(), "window_end": end.isoformat()}
    offered = offered_baseline(profile, baseline.get("metrics", {}), start, end)
    application = metric_summary(profile, baseline.get("metrics", {}), start, end)
    rollout = baseline.get("rollout", {})
    adequate_rollout = rollout.get("coverage") == "complete" and rollout.get("health") in {
        "healthy",
        "degraded",
        "progressing",
    }
    result = None
    if offered is None or not adequate_rollout:
        result = denied(
            "Fresh comparable offered traffic and complete pre-change rollout evidence are required."
        )
    elif (
        plan.content.get("direction") == "forward"
        and rollout["health"] == "healthy"
        and application["state"] == "passed"
    ):
        result = denied(
            "The qualified target is already recovered; no remediation was dispatched.", no_op=True
        )
    elif (
        plan.content.get("direction") == "forward"
        and rollout["health"] == "healthy"
        and application["state"] != "failed"
    ):
        result = denied(
            "The pre-change symptom is not established by qualified application evidence."
        )
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id, with_for_update=True)
        verification = db.get(Verification, row.id, with_for_update=True)
        if row.maintenance_released or row.dispatch_started_at is not None:
            return denied("Execution authority changed during the pre-change observation.")
        verification.baseline = baseline
        if result is not None:
            verification.result = {
                **result,
                "state": "no_op" if result.get("code") == "no_op" else "inconclusive",
            }
            verification.finalized_at = verification.finalized_at or database_now(db)
    return result


def finalize(db, execution, verification, result):
    if verification.finalized_at is not None:
        return
    verification.result = result
    verification.finalized_at = database_now(db)
    state = result.get("state", "inconclusive")
    plan = db.get(ActionPlan, execution.plan_id)
    execution.state = (
        ("rolled_back" if plan.content.get("direction") == "inverse" else "succeeded")
        if state == "passed"
        else "failed"
        if state == "failed"
        else "manual_intervention"
    )
    execution.holds_lock = False
    execution.finished_at = verification.finalized_at
    execution.result = {
        **execution.result,
        "verification": state,
        "verification_finalized_at": verification.finalized_at.isoformat(),
        "verification_profile_digest": verification.profile_digest,
        "verification_result": result,
        "reason": "Qualified post-change checks passed; inspect the independent observations."
        if state == "passed"
        else "Qualified post-change conditions failed."
        if state == "failed"
        else "Verification evidence is inconclusive; investigate or hand off before a fresh change.",
    }
    from cloudops.features.recovery.rollback import arm

    arm(db, execution, verification)


def retire(db, execution, reason):
    row = db.get(Verification, execution.id, with_for_update=True)
    if row is not None and row.finalized_at is None:
        row.result = {"state": "inconclusive", "reason_codes": [reason]}
        row.finalized_at = database_now(db)
        execution.result = {
            **execution.result,
            "verification": "inconclusive",
            "verification_finalized_at": row.finalized_at.isoformat(),
            "verification_result": row.result,
        }


def begin(db, execution, result):
    verification = db.get(Verification, execution.id, with_for_update=True)
    if (
        verification is None
        or verification.baseline is None
        or verification.finalized_at is not None
    ):
        return False
    try:
        profile = VerificationProfile.model_validate(verification.profile)
        ack = datetime.fromisoformat(result["acknowledged_at"].replace("Z", "+00:00"))
        now = database_now(db)
        if ack < execution.dispatch_started_at or ack > now + timedelta(seconds=1):
            raise ValueError("Acknowledgement clock is outside call admission.")
    except (KeyError, TypeError, ValueError):
        finalize(
            db,
            execution,
            verification,
            {"state": "inconclusive", "reason_codes": ["acknowledgement_anchor_unavailable"]},
        )
        return True
    if verification.acknowledged_at is None:
        verification.acknowledged_at = ack
        verification.deadline = ack + timedelta(seconds=profile.window_seconds)
    if verification.acknowledged_at != ack:
        finalize(
            db,
            execution,
            verification,
            {"state": "inconclusive", "reason_codes": ["acknowledgement_anchor_changed"]},
        )
        return True
    execution.state, execution.holds_lock, execution.finished_at = "verifying", True, None
    execution.result = {
        **result,
        "verification": "pending",
        "reason": "Acknowledged change; fixed-window qualified post-checks are pending.",
    }
    if not db.scalar(
        select(Job.id).where(Job.kind == VERIFICATION_JOB, Job.subject_id == execution.id)
    ):
        db.add(
            Job(
                kind=VERIFICATION_JOB,
                subject_id=execution.id,
                due_at=ack,
                max_attempts=profile.window_seconds // 30 + 3,
            )
        )
    return True


class VerificationHandler:
    def eligible(self, db, subject_id):
        row = db.get(Execution, subject_id, with_for_update=True)
        verification = db.get(Verification, subject_id, with_for_update=True)
        return bool(
            row
            and not row.maintenance_released
            and row.state == "verifying"
            and verification
            and verification.finalized_at is None
        )

    def started(self, db, subject_id):
        pass

    def failed(self, db, subject_id, reason, terminal):
        row = db.get(Execution, subject_id, with_for_update=True)
        verification = db.get(Verification, subject_id, with_for_update=True)
        if (
            row
            and verification
            and row.state == "verifying"
            and not row.maintenance_released
            and terminal
        ):
            finalize(
                db,
                row,
                verification,
                {
                    "state": "inconclusive",
                    "reason_codes": ["verification_attempt_budget_exhausted"],
                },
            )

    def publish(self, db, subject_id, result):
        raise RuntimeError("Verification uses its fenced slot runner.")


def verify(claim, gate, lease_lost):
    from cloudops.features.recovery.reconciliation import done

    def before_call():
        if lease_lost.is_set() or not start_call(claim, gate):
            raise Interrupted()

    with gate.admitted() as admission, sessions()() as db, db.begin():
        pair = _fenced(db, claim, admission)
        if pair is None:
            raise Interrupted()
        job, _ = pair
        execution = db.get(Execution, claim.subject_id, with_for_update=True)
        row = db.get(Verification, claim.subject_id, with_for_update=True)
        if (
            row.finalized_at is not None
            or execution.maintenance_released
            or execution.state != "verifying"
        ):
            done(db, job)
            return
        plan = db.get(ActionPlan, execution.plan_id)
        try:
            profile = require_plan(plan.content)
            if profile.digest() != row.profile_digest:
                raise ValueError("Profile changed.")
        except (DomainError, ValueError):
            finalize(
                db,
                execution,
                row,
                {"state": "inconclusive", "reason_codes": ["qualified_policy_or_sources_changed"]},
            )
            done(db, job)
            return
        now = database_now(db)
        if now > row.deadline + timedelta(seconds=profile.sample_lateness_seconds):
            finalize(
                db,
                execution,
                row,
                {"state": "inconclusive", "reason_codes": ["fixed_verification_deadline_elapsed"]},
            )
            done(db, job)
            return
        slot = max(row.next_slot, int((now - row.acknowledged_at).total_seconds()) // 30)
        last_slot = profile.window_seconds // 30
        if slot > last_slot:
            finalize(
                db,
                execution,
                row,
                {"state": "inconclusive", "reason_codes": ["original_slot_budget_exhausted"]},
            )
            done(db, job)
            return
        due = row.acknowledged_at + timedelta(seconds=slot * 30)
        if now < due:
            defer_owned(db, claim, due)
            return
        row.next_slot = slot + 1
        workload = db.get(Workload, execution.workload_id)
        if not workload.active or any(
            getattr(workload, key) != plan.content["workload"][key]
            for key in ("cluster", "namespace", "name", "uid")
        ):
            finalize(
                db,
                execution,
                row,
                {"state": "inconclusive", "reason_codes": ["original_active_target_unavailable"]},
            )
            done(db, job)
            return
        start, end, incident_id = row.acknowledged_at, row.deadline, plan.incident_id
    before_call()
    sample = bounded(
        sources.capture(
            execution.id,
            incident_id,
            workload,
            start,
            end,
            before_call=before_call,
            profile=profile if slot == last_slot else None,
        )
    )
    with gate.admitted() as admission, sessions()() as db, db.begin():
        pair = _fenced(db, claim, admission)
        if pair is None:
            raise Interrupted()
        job, _ = pair
        execution = db.get(Execution, claim.subject_id, with_for_update=True)
        row = db.get(Verification, claim.subject_id, with_for_update=True)
        if (
            row.finalized_at is not None
            or execution.maintenance_released
            or execution.state != "verifying"
        ):
            done(db, job)
            return
        try:
            current_plan = db.get(ActionPlan, execution.plan_id)
            require_plan(current_plan.content)
        except DomainError:
            finalize(
                db,
                execution,
                row,
                {
                    "state": "inconclusive",
                    "reason_codes": ["qualified_policy_or_sources_changed_during_read"],
                },
            )
            done(db, job)
            return
        if str(slot) not in row.samples:
            row.samples = {**row.samples, str(slot): sample}
        if slot == last_slot:
            plan = db.get(ActionPlan, execution.plan_id)
            result = evaluate(
                profile,
                row.acknowledged_at,
                plan.content["desired"],
                row.baseline,
                row.samples,
                sample.get("metrics", {}),
            )
            finalize(db, execution, row, result)
            done(db, job)
        else:
            defer_owned(db, claim, row.acknowledged_at + timedelta(seconds=(slot + 1) * 30))
