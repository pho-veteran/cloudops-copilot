import hashlib
import json
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from cloudops.features.access.service import token_hash
from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.states import nonterminal_run
from cloudops.features.recovery import client
from cloudops.features.recovery.classification import BLOCKING, classify
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.recovery.outcomes import (
    RECONCILE_ATTEMPTS,
    RECONCILE_JOB,
    RECONCILE_SECONDS,
)
from cloudops.features.workloads.kubernetes import Kubernetes, snapshot
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now, sessions
from cloudops.shared.config import settings
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, retire_queued


def _available_locally(db, workload):
    if workload.source != "live_kubernetes":
        raise DomainError(
            409, "live_target_required", "A synthetic workload cannot produce an executable plan."
        )
    executions = db.execute(
        select(
            Execution.state, Execution.holds_lock, Execution.maintenance_released, Execution.result
        ).where(Execution.workload_id == workload.id)
    )
    if any(
        row.holds_lock
        or classify(row.state, row.holds_lock, row.maintenance_released, row.result) in BLOCKING
        for row in executions
    ):
        raise DomainError(
            409,
            "target_locked",
            "An active, locked or unclassified execution blocks this target. Inspect its evidence and maintenance state.",
        )
    if db.scalar(
        select(Execution.id).where(
            Execution.workload_id == workload.id,
            Execution.created_at > database_now(db) - timedelta(seconds=30),
        )
    ):
        raise DomainError(
            409,
            "target_cooldown",
            "Wait thirty seconds after the last admitted execution and inspect fresh live state before another proposal.",
        )


def _require_authority(authority):
    if not authority["enabled"] or authority["recovery_mode"]:
        raise DomainError(
            409,
            "target_quarantined",
            "A deployment operator must establish proof and explicitly enable this target.",
        )


def available(db, workload):
    _available_locally(db, workload)
    authority = client.target(workload.uid)
    _require_authority(authority)
    return authority


def propose(db, incident_id, actor_id, replicas, rationale):
    with db.begin():
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if not incident or incident.state == "resolved":
            raise DomainError(
                409, "incident_unavailable", "Reopen a saved incident before proposing a plan."
            )
        workload = require_active(db, incident.workload_id)
        authority = available(db, workload)
        kube = Kubernetes()
        deployment = kube.deployment(workload)
        kube.controllers(workload)
        kube.capacity(deployment, replicas)
        before = snapshot(deployment)
        if before["replicas"] == replicas:
            raise DomainError(
                409,
                "no_op",
                "The target already has that replica count; no executable change is needed.",
            )
        invalidate(db, incident_id)
        now = database_now(db)
        content = {
            "source": "operator_proposal_with_live_kubernetes_observation",
            "workload": {k: getattr(workload, k) for k in ("cluster", "namespace", "name", "uid")},
            "resource_version": deployment["metadata"]["resourceVersion"],
            "before": before,
            "replicas": replicas,
            "template": "scale_deployment",
            "target_epoch": authority["epoch"],
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "rationale": rationale,
            "verification": "Kubernetes acknowledgement only; observe rollout and application metrics manually. No remediation-success claim.",
            "rollback": "unavailable; no automatic inverse change",
        }
        plan = ActionPlan(
            origin="operator_manual",
            incident_id=incident_id,
            workload_id=workload.id,
            content=content,
            digest=hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
            target_epoch=authority["epoch"],
            rationale=rationale,
            created_by=actor_id,
            expires_at=now + timedelta(minutes=5),
        )
        db.add(plan)
        db.flush()
        return plan


def invalidate(db, incident_id):
    db.execute(
        update(ActionPlan)
        .where(ActionPlan.incident_id == incident_id, ActionPlan.state == "proposed")
        .values(state="invalidated")
    )


def retire_unsent(db, incident_id):
    executions = db.scalars(
        select(Execution)
        .join(ActionPlan, ActionPlan.id == Execution.plan_id)
        .where(ActionPlan.incident_id == incident_id, Execution.state == "queued")
        .order_by(Execution.id)
    ).all()
    for execution in executions:
        if execution.dispatch_started_at is not None or not retire_queued(
            db, "recovery_execution_v1", execution.id, "Invalidated before dispatch claim."
        ):
            continue
        db.refresh(execution, with_for_update=True)
        if execution.state != "queued" or execution.dispatch_started_at is not None:
            raise DomainError(409, "execution_active", "Dispatch claim won; inspect its outcome.")
        execution.state, execution.holds_lock = "manual_intervention", False
        record_outcome(
            execution,
            {
                "outcome": "denied",
                "attribution": "not_sent",
                "verification": "not_performed",
                "code": "invalidated_before_claim",
                "reason": "The unclaimed execution was retired before any gateway call.",
            },
        )
        execution.finished_at = database_now(db)
        db.get(ActionPlan, execution.plan_id).state = "invalidated"
    db.flush()


def read(db, plan_id, session):
    with db.begin():
        plan = db.get(ActionPlan, plan_id)
        if not plan:
            raise DomainError(404, "plan_not_found", "Plan not found.")
        now = database_now(db)
        valid, reason = False, "This plan is decided or expired. Prepare and read a new exact plan."
        if plan.state == "proposed" and plan.expires_at > now:
            try:
                if plan.content.get("schema_version") == "RecoveryPlan/v2":
                    raise DomainError(
                        409,
                        "execution_unavailable",
                        "Preview only: matching gateway, lifecycle and qualified verification capability are unavailable. Prepare a new plan when enabled.",
                    )
                workload = db.get(Workload, plan.workload_id)
                if not workload or not workload.active:
                    raise DomainError(
                        409, "target_changed", "Target scope changed. Prepare a new plan."
                    )
                authority = available(db, workload)
                if authority["epoch"] != plan.target_epoch:
                    raise DomainError(
                        409, "plan_changed", "Maintenance retired this plan's target authority."
                    )
                kube = Kubernetes()
                current = kube.deployment(workload)
                kube.controllers(workload)
                kube.capacity(current, plan.content["replicas"])
                if snapshot(current) != plan.content["before"]:
                    raise DomainError(
                        409,
                        "plan_changed",
                        "Live target fields changed after preview. Prepare a new plan.",
                    )
                valid, reason = (
                    True,
                    "Live target, authority and immutable plan are current; an explicit decision is still required.",
                )
            except DomainError as error:
                reason = error.message
        db.execute(
            delete(PlanReview).where(PlanReview.plan_id == plan.id, PlanReview.expires_at <= now)
        )
        token = secrets.token_urlsafe(32)
        db.add(
            PlanReview(
                plan_id=plan.id,
                token_hash=token_hash(token),
                session_binding=token_hash(session.csrf_token),
                expires_at=min(plan.expires_at, now + timedelta(seconds=60)),
            )
        )
        return plan, token, valid, reason


def _decision_rows(db, plan_id, session, body, *, locked):
    """Validate both before live probes and again under the short commit locks."""

    def query(statement):
        return db.scalar(statement.with_for_update() if locked else statement)

    incident_id = db.scalar(select(ActionPlan.incident_id).where(ActionPlan.id == plan_id))
    if incident_id is None:
        raise DomainError(404, "plan_not_found", "Plan not found.")
    incident = query(select(Incident).where(Incident.id == incident_id))
    plan = query(select(ActionPlan).where(ActionPlan.id == plan_id))
    if plan.version != body.version or not secrets.compare_digest(plan.digest, body.digest):
        raise DomainError(
            409, "plan_changed", "Read the exact plan version and diff before deciding."
        )
    now = database_now(db)
    if plan.state != "proposed" or plan.expires_at <= now or incident.state == "resolved":
        raise DomainError(
            409,
            "plan_unavailable",
            "This plan is decided, expired or invalidated. Start a new investigation; no decision was replayed.",
        )
    review = query(
        select(PlanReview).where(
            PlanReview.plan_id == plan_id,
            PlanReview.token_hash == token_hash(body.review_token),
        )
    )
    if (
        not review
        or review.consumed
        or review.expires_at <= now
        or review.session_binding != token_hash(session.csrf_token)
    ):
        raise DomainError(
            409,
            "plan_refetch_required",
            "Read the current plan after signing in and make a fresh explicit decision.",
        )
    if body.decision == "approve" and nonterminal_run(db, incident_id):
        raise DomainError(
            409,
            "run_active",
            "Wait for the pending investigation to finish, or cancel it, before approving.",
        )
    if body.decision == "approve" and plan.content.get("schema_version") == "RecoveryPlan/v2":
        raise DomainError(
            409,
            "execution_unavailable",
            "This immutable preview cannot dispatch until matching recovery capabilities are available. Prepare a new plan when enabled.",
        )
    workload = query(select(Workload).where(Workload.id == plan.workload_id))
    if not workload or not workload.active:
        raise DomainError(
            422,
            "workload_unavailable",
            "Choose an active configured workload; historical identities cannot admit new work.",
        )
    _available_locally(db, workload)
    if body.decision == "approve" and (
        db.scalar(select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"])))
        >= settings().queue_limit
    ):
        raise DomainError(
            409, "queue_full", "The bounded work queue is full; no execution was admitted."
        )
    return incident, plan, review, workload, now


def decide(db, plan_id, session, body):
    # Read-only preflight grants no authority and takes no admission or row locks.
    with db.begin():
        _, plan, _, workload, _ = _decision_rows(db, plan_id, session, body, locked=False)
        target_identity = (
            workload.id,
            workload.cluster,
            workload.namespace,
            workload.name,
            workload.uid,
        )
        plan_identity = (plan.version, plan.digest, plan.target_epoch, plan.workload_id)
        replicas = plan.content.get("replicas")
    authority = client.target(workload.uid)
    _require_authority(authority)
    current = None
    if body.decision == "approve":
        kube = Kubernetes()
        current = kube.deployment(workload)
        kube.controllers(workload)
        kube.capacity(current, replicas)
    # Expire preflight ORM objects: expire_on_commit=False must not conceal a
    # concurrent resolve, receipt consumption, scope retirement or plan change.
    db.expire_all()
    with db.begin():
        if body.decision == "approve":
            lock_admission(db)
        _, plan, review, workload, now = _decision_rows(db, plan_id, session, body, locked=True)
        if (
            (workload.id, workload.cluster, workload.namespace, workload.name, workload.uid)
            != target_identity
            or (plan.version, plan.digest, plan.target_epoch, plan.workload_id) != plan_identity
            or authority["epoch"] != plan.target_epoch
        ):
            raise DomainError(
                409,
                "plan_changed",
                "Maintenance changed target authority. Start a new investigation and prepare a new plan.",
            )
        if body.decision == "approve":
            if snapshot(current) != plan.content["before"]:
                raise DomainError(
                    409,
                    "plan_changed",
                    "The live target changed after preview. Start a new investigation.",
                )
            # The executor independently rechecks epoch/UID/resourceVersion at
            # dispatch; these unlocked observations never authorize a stale write.
            execution = Execution(
                plan_id=plan.id,
                workload_id=workload.id,
                result={"dispatch_resource_version": current["metadata"]["resourceVersion"]},
            )
            db.add(execution)
            db.flush()
            execution.dispatch_request = dispatch_request(plan, execution, current)
            db.add(Job(kind="recovery_execution_v1", subject_id=execution.id))
        review.consumed = True
        plan.state = "approved" if body.decision == "approve" else "rejected"
        plan.decided_by, plan.decided_at = session.principal.id, now
        db.flush()
        return plan


def dispatch_request(plan, execution, current=None):
    captured = execution.result.get("dispatch_resource_version")
    if captured is None:
        captured = plan.content.get("resource_version")
    if current is not None:
        captured = current["metadata"]["resourceVersion"]
    if plan.content.get("schema_version") == "RecoveryPlan/v2":
        from cloudops.features.recovery.dispatch import RecoveryDispatch

        return RecoveryDispatch(
            schema_version="RecoveryDispatch/v2",
            operation_id=execution.id,
            execution_id=execution.id,
            direction=plan.content["direction"],
            template=plan.template,
            plan_id=plan.id,
            plan_version=plan.version,
            plan_digest=plan.digest,
            uid=plan.content["workload"]["uid"],
            epoch=plan.target_epoch,
            resource_version=captured,
            expires_at=plan.expires_at,
            policy=plan.content["policy"],
            before=plan.content["before"],
            desired=plan.content["desired"],
            selector=plan.content["selector"],
        ).model_dump(mode="json")
    return {
        "execution_id": str(execution.id),
        "uid": plan.content["workload"]["uid"],
        "epoch": plan.target_epoch,
        "resource_version": captured,
        "replicas": plan.content["replicas"],
        "before": plan.content["before"],
        "plan_digest": plan.digest,
        "expires_at": plan.expires_at.isoformat(),
    }


def unknown(reason):
    return {
        "outcome": "outcome_unknown",
        "attribution": "outcome_unknown",
        "verification": "inconclusive",
        "reason": reason,
    }


def receipt_result(receipt, execution_id, request=None):
    if not isinstance(receipt, dict):
        raise ValueError("Unsupported gateway receipt.")
    version_two = request is not None and request.get("schema_version") == "RecoveryDispatch/v2"
    if version_two and receipt.get("schema_version") != "RecoveryReceipt/v2":
        raise ValueError("A v2 operation requires its exact typed receipt.")
    if (
        request is not None
        and not version_two
        and receipt.get("schema_version") == "RecoveryReceipt/v2"
    ):
        raise ValueError("A legacy operation cannot change receipt protocol.")
    if receipt.get("schema_version") == "RecoveryReceipt/v2":
        from cloudops.features.recovery.dispatch import RecoveryReceipt

        value = RecoveryReceipt.model_validate(receipt)
        if value.operation_id != execution_id:
            raise ValueError("Receipt operation identity changed.")
        if value.attribution == "acknowledged" and (
            not value.acknowledged_resource_version or value.acknowledged_at is None
        ):
            raise ValueError("Acknowledged operation metadata is unavailable.")
        if version_two and (
            str(value.plan_id) != request["plan_id"] or value.plan_digest != request["plan_digest"]
        ):
            raise ValueError("Receipt plan identity changed.")
        return {
            **receipt,
            "outcome": value.attribution,
            "verification": "not_performed"
            if value.attribution in {"denied", "not_sent"}
            else "inconclusive",
            "gateway_receipt": receipt,
        }
    if receipt.get("outcome") not in {"acknowledged", "denied", "outcome_unknown"}:
        raise ValueError("Unsupported gateway receipt.")
    return receipt


def lookup(execution_id, request=None):
    try:
        return receipt_result(client.operation(execution_id), execution_id, request)
    except (DomainError, ValueError, TypeError, KeyError):
        return unknown(
            "No exact durable gateway receipt is available. Missing receipt does not establish no write; reconciliation is read-only."
        )


def admit_receipt_read(db, row):
    if (
        row.reconcile_deadline is None
        or row.reconcile_deadline <= database_now(db)
        or row.reconcile_attempts >= RECONCILE_ATTEMPTS
    ):
        return False
    row.reconcile_attempts += 1
    return True


def execute(execution_id, *, before_call):
    with sessions()() as db, db.begin():
        original = db.get(Execution, execution_id)
        db.scalar(select(Workload).where(Workload.id == original.workload_id).with_for_update())
        execution = db.scalar(
            select(Execution)
            .where(Execution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        plan = db.get(ActionPlan, execution.plan_id)
        if execution.maintenance_released:
            return execution.result or unknown("Trusted maintenance retired this authority.")
        if execution.state in {
            "manual_intervention",
            "succeeded",
            "failed",
            "no_op",
            "rolled_back",
        }:
            return execution.result
        already_started = execution.dispatch_started_at is not None
        if already_started and not admit_receipt_read(db, execution):
            return unknown(
                "Persisted receipt-read budget exhausted; keep target quarantine without another external request."
            )
        if not already_started and plan.expires_at <= database_now(db):
            return {
                "outcome": "denied",
                "attribution": "not_sent",
                "verification": "not_performed",
                "reason": "Plan expired before execution; prepare a new investigation and plan.",
            }
        request = execution.dispatch_request or dispatch_request(plan, execution)
        if not already_started:
            execution.dispatch_request = request
    before_call()
    if already_started:
        return lookup(execution_id, request)
    with sessions()() as db, db.begin():
        original = db.get(Execution, execution_id)
        workload = db.scalar(
            select(Workload).where(Workload.id == original.workload_id).with_for_update()
        )
        execution = db.scalar(
            select(Execution)
            .where(Execution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if execution.maintenance_released:
            return execution.result or unknown("Trusted maintenance retired this authority.")
        already_started = execution.dispatch_started_at is not None
        if already_started and not admit_receipt_read(db, execution):
            return unknown(
                "Persisted receipt-read budget exhausted; keep target quarantine without another external request."
            )
        if not already_started:
            now = database_now(db)
            plan = db.get(ActionPlan, execution.plan_id)
            if (
                execution.state not in {"executing", "rolling_back"}
                or not execution.holds_lock
                or plan.state != "approved"
                or plan.expires_at <= now
                or execution.dispatch_request != request
                or not workload.active
            ):
                return {
                    "outcome": "denied",
                    "attribution": "not_sent",
                    "verification": "not_performed",
                    "reason": "The exact claimed approval is no longer available before call admission.",
                }
            execution.dispatch_started_at = now
            execution.reconcile_deadline = now + timedelta(seconds=RECONCILE_SECONDS)
    if already_started:
        return lookup(execution_id, request)
    before_call()
    try:
        return receipt_result(client.gateway("/dispatch", request), execution_id, request)
    except (DomainError, ValueError, TypeError, KeyError):
        return unknown(
            "Gateway response unavailable after call admission; no mutation retry is permitted."
        )


def record_outcome(row, result):
    captured = row.result.get("dispatch_resource_version")
    row.result = {**result, **({"dispatch_resource_version": captured} if captured else {})}


class ExecutionHandler:
    def eligible(self, db, subject_id):
        execution = db.get(Execution, subject_id)
        if execution is None:
            return False
        plan = db.get(ActionPlan, execution.plan_id)
        incident = db.scalar(
            select(Incident).where(Incident.id == plan.incident_id).with_for_update()
        )
        db.refresh(plan, with_for_update=True)
        workload = db.scalar(
            select(Workload).where(Workload.id == execution.workload_id).with_for_update()
        )
        db.refresh(execution, with_for_update=True)
        if execution.maintenance_released or execution.state not in {
            "queued",
            "executing",
            "reconciling",
            "outcome_unknown",
        }:
            return False
        if execution.dispatch_started_at is not None:
            return True
        if (
            plan.state != "approved"
            or incident.state == "resolved"
            or not workload.active
            or plan.expires_at <= database_now(db)
        ):
            execution.state, execution.holds_lock = "manual_intervention", False
            execution.finished_at = database_now(db)
            record_outcome(
                execution,
                {
                    "outcome": "denied",
                    "attribution": "not_sent",
                    "verification": "not_performed",
                    "reason": "The exact approval, original active target or expiry is unavailable before claim.",
                },
            )
            return False
        return True

    def started(self, db, subject_id):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if not row.maintenance_released:
            row.state = "reconciling" if row.dispatch_started_at is not None else "executing"

    def failed(self, db, subject_id, reason, terminal):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if row.maintenance_released:
            return
        started = row.dispatch_started_at is not None
        row.state = "outcome_unknown" if started else "manual_intervention"
        row.holds_lock = started
        record_outcome(
            row,
            {
                "outcome": "outcome_unknown" if started else "denied",
                "attribution": "outcome_unknown" if started else "not_sent",
                "verification": "inconclusive" if started else "not_performed",
                "reason": "The worker could not publish its execution receipt; no functional replay is permitted.",
            },
        )
        if terminal or not started:
            row.finished_at = database_now(db)

    def publish(self, db, subject_id, result):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if row.maintenance_released:
            return
        record_outcome(row, result)
        row.state = (
            "outcome_unknown" if result["outcome"] == "outcome_unknown" else "manual_intervention"
        )
        row.holds_lock = row.state == "outcome_unknown"
        row.finished_at = database_now(db)
        if (
            row.state == "outcome_unknown"
            and row.reconcile_deadline is not None
            and row.reconcile_deadline > database_now(db)
            and row.reconcile_attempts < RECONCILE_ATTEMPTS
        ):
            row.state, row.finished_at = "reconciling", None
            if not db.scalar(
                select(Job.id).where(Job.kind == RECONCILE_JOB, Job.subject_id == row.id)
            ):
                db.add(Job(kind=RECONCILE_JOB, subject_id=row.id, max_attempts=RECONCILE_ATTEMPTS))
