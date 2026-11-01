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
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.workloads.kubernetes import Kubernetes, snapshot
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now, sessions
from cloudops.shared.config import settings
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job


def _available_locally(db, workload):
    if workload.source != "live_kubernetes":
        raise DomainError(
            409, "live_target_required", "A synthetic workload cannot produce an executable plan."
        )
    if db.scalar(
        select(Execution.id).where(Execution.workload_id == workload.id, Execution.holds_lock)
    ):
        raise DomainError(
            409,
            "target_locked",
            "An execution or historical unknown still holds this target. Inspect its evidence and maintenance state.",
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


def read(db, plan_id, session):
    with db.begin():
        plan = db.get(ActionPlan, plan_id)
        if not plan:
            raise DomainError(404, "plan_not_found", "Plan not found.")
        now = database_now(db)
        valid, reason = False, "This plan is decided or expired. Prepare and read a new exact plan."
        if plan.state == "proposed" and plan.expires_at > now:
            try:
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
                if (
                    current["metadata"]["resourceVersion"] != plan.content["resource_version"]
                    or snapshot(current) != plan.content["before"]
                ):
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
            if (
                current["metadata"]["resourceVersion"] != plan.content["resource_version"]
                or snapshot(current) != plan.content["before"]
            ):
                raise DomainError(
                    409,
                    "plan_changed",
                    "The live target changed after preview. Start a new investigation.",
                )
            # The executor independently rechecks epoch/UID/resourceVersion at
            # dispatch; these unlocked observations never authorize a stale write.
            execution = Execution(plan_id=plan.id, workload_id=workload.id)
            db.add(execution)
            db.flush()
            db.add(Job(kind="recovery_execution_v1", subject_id=execution.id))
        review.consumed = True
        plan.state = "approved" if body.decision == "approve" else "rejected"
        plan.decided_by, plan.decided_at = session.principal.id, now
        db.flush()
        return plan


def execute(execution_id):
    with sessions()() as db:
        execution = db.get(Execution, execution_id)
        plan = db.get(ActionPlan, execution.plan_id)
        if execution.maintenance_released:
            return {
                "outcome": "outcome_unknown",
                "verification": "inconclusive",
                "reason": "Trusted maintenance retired this authority; historical attribution remains unknown.",
            }
        if plan.expires_at <= database_now(db):
            return {
                "outcome": "denied",
                "verification": "not_performed",
                "reason": "Plan expired before execution; prepare a new investigation and plan.",
            }
        request = {
            "execution_id": str(execution.id),
            "uid": plan.content["workload"]["uid"],
            "epoch": plan.target_epoch,
            "resource_version": plan.content["resource_version"],
            "replicas": plan.content["replicas"],
            "before": plan.content["before"],
            "plan_digest": plan.digest,
            "expires_at": plan.expires_at.isoformat(),
        }
    try:
        return client.gateway("/dispatch", request)
    except DomainError:
        return {
            "outcome": "outcome_unknown",
            "verification": "inconclusive",
            "reason": "Executor response unavailable. Keep the target lock and use trusted maintenance proof; no blind repeat write is permitted.",
        }


class ExecutionHandler:
    def started(self, db, subject_id):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if not row.maintenance_released:
            row.state = "executing"

    def failed(self, db, subject_id, reason, terminal):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if row.maintenance_released:
            return
        row.state, row.holds_lock = "outcome_unknown", True
        row.result = {
            "outcome": "outcome_unknown",
            "verification": "inconclusive",
            "reason": "The worker could not publish its execution receipt; trusted reconciliation is required.",
        }

    def publish(self, db, subject_id, result):
        row = db.scalar(select(Execution).where(Execution.id == subject_id).with_for_update())
        if row.maintenance_released:
            return
        row.result = result
        row.state = (
            "outcome_unknown" if result["outcome"] == "outcome_unknown" else "manual_intervention"
        )
        row.holds_lock = row.state == "outcome_unknown"
        row.finished_at = database_now(db)
