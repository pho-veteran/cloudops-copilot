"""Separately approved inverses of exact acknowledged operations, never clock re-arming."""

from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select

from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.locators import digest
from cloudops.features.recovery import policy
from cloudops.features.recovery.dispatch import RecoveryDispatch
from cloudops.features.recovery.fields import Ineligible, safe_resources
from cloudops.features.recovery.models import ActionPlan, Execution, RollbackWindow, Verification
from cloudops.features.recovery.verification_policy import load_for
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError

INVERSE_POLICY = "captured-old-state/v1"


def deny(reason):
    raise DomainError(409, "inverse_unavailable", reason)


def safe_old_state(rules, uid, template, desired):
    safe_resources(desired["containers"], rules)
    if not 1 <= desired["replicas"] <= 3:
        deny("The captured replica count is outside the supported serving range.")
    if template != "scale_deployment" and not any(
        state.workload_uid == uid
        and sorted(
            [c.model_dump(mode="json", exclude_none=True) for c in state.containers],
            key=lambda c: c["name"],
        )
        == desired["containers"]
        for state in rules.inverse_qualified_states
    ):
        deny("The captured old template/resources lack independent inverse qualification.")
    if template == "restore_deployment_template" and any(
        c["image"] not in rules.qualified_images for c in desired["containers"]
    ):
        deny("The captured old image is not independently qualified.")


def contract(content, rules, *, verified_profile_digest=None):
    if rules.inverse_policy != INVERSE_POLICY or rules.inverse_window_seconds is None:
        return None
    try:
        safe_old_state(rules, content["workload"]["uid"], content["template"], content["before"])
        if verified_profile_digest is None:
            profile = load_for(content["workload"]["uid"], content["template"], content["before"])
            if profile is None or profile.id != rules.verification_policy:
                return None
            verified_profile_digest = profile.digest()
        return {
            "policy": INVERSE_POLICY,
            "window_seconds": rules.inverse_window_seconds,
            "verification_profile_digest": verified_profile_digest,
            "safety_digest": digest(
                {
                    key: value
                    for key, value in rules.model_dump(mode="json").items()
                    if key != "memory_profiles"
                }
            ),
        }
    except (DomainError, Ineligible, ValueError, KeyError, TypeError):
        return None


def acknowledged(execution, plan):
    from cloudops.features.recovery.service import receipt_result

    try:
        request = RecoveryDispatch.model_validate(execution.dispatch_request)
        receipt = receipt_result(
            execution.result["gateway_receipt"], execution.id, execution.dispatch_request
        )
        if (
            request.direction != "forward"
            or plan.state != "approved"
            or request.plan_version != plan.version
            or request.plan_id != plan.id
            or request.plan_digest != plan.digest
            or digest(plan.content) != plan.digest
            or request.before.selected() != plan.content["before"]
            or request.desired.selected() != plan.content["desired"]
            or request.uid != plan.content["workload"]["uid"]
            or request.template != plan.template
            or request.epoch != plan.target_epoch
            or request.policy.model_dump(mode="json") != plan.content["policy"]
            or request.selector.model_dump(mode="json") != plan.content["selector"]
            or execution.dispatch_started_at is None
            or execution.result.get("attribution", execution.result.get("outcome"))
            != "acknowledged"
            or receipt["outcome"] != "acknowledged"
        ):
            deny("An exact durable acknowledged forward operation is required.")
        return request
    except (KeyError, TypeError, ValueError):
        deny("An exact durable acknowledged forward operation is required.")


def arm(db, execution, verification):
    plan = db.get(ActionPlan, execution.plan_id)
    captured = plan.content.get("rollback_contract")
    now = verification.finalized_at
    if (
        not captured
        or plan.content.get("direction") != "forward"
        or execution.maintenance_released
        or verification.deadline is None
        or verification.result.get("state") not in {"passed", "failed", "inconclusive"}
        or now < verification.deadline
        or db.get(RollbackWindow, execution.id) is not None
    ):
        return
    try:
        acknowledged(execution, plan)
        if verification.acknowledged_at != datetime.fromisoformat(
            execution.result["gateway_receipt"]["acknowledged_at"].replace("Z", "+00:00")
        ):
            return
        rules = policy.load()
        if rules is None or captured != contract(plan.content, rules):
            return
        deadline = min(now, verification.deadline) + timedelta(seconds=captured["window_seconds"])
        if deadline <= now:
            return
        db.add(
            RollbackWindow(
                forward_execution_id=execution.id,
                eligible_from=now,
                deadline=deadline,
                policy_digest=digest(captured),
            )
        )
    except DomainError:
        return


def require_forward(db, identifier, *, locked=False, check_sources=True):
    execution = db.get(Execution, identifier)
    if execution is None:
        deny("The original forward execution is unavailable.")
    plan = db.get(ActionPlan, execution.plan_id)
    window = db.get(RollbackWindow, identifier, with_for_update=locked)
    verification = db.get(Verification, identifier)
    if (
        window is None
        or verification is None
        or verification.finalized_at != window.eligible_from
        or verification.deadline is None
        or window.eligible_from < verification.deadline
        or window.eligible_from > database_now(db)
        or execution.maintenance_released
        or execution.holds_lock
        or execution.state not in {"succeeded", "failed", "manual_intervention"}
        or window.deadline <= database_now(db)
    ):
        deny("No current original rollback window exists; inspect history or use manual handoff.")
    forward = acknowledged(execution, plan)
    rules = policy.load()
    captured = plan.content.get("rollback_contract")
    if not isinstance(captured, dict) or not isinstance(
        captured.get("verification_profile_digest"), str
    ):
        deny("The original inverse qualification is unavailable.")
    current_contract = (
        contract(
            plan.content,
            rules,
            verified_profile_digest=None
            if check_sources
            else captured["verification_profile_digest"],
        )
        if rules
        else None
    )
    if (
        rules is None
        or digest(plan.content.get("rollback_contract")) != window.policy_digest
        or captured != current_contract
        or window.deadline
        != verification.deadline
        + timedelta(seconds=plan.content["rollback_contract"]["window_seconds"])
        or verification.acknowledged_at
        != datetime.fromisoformat(
            execution.result["gateway_receipt"]["acknowledged_at"].replace("Z", "+00:00")
        )
    ):
        deny("The original inverse policy or qualified old-state evidence changed.")
    return execution, plan, window, forward


def require_inverse(db, plan, execution=None, *, locked=False, check_sources=True):
    if plan.content.get("direction") != "inverse":
        return None
    if plan.forward_execution_id is None:
        deny("The immutable forward link is unavailable.")
    forward_execution, original, window, forward = require_forward(
        db, plan.forward_execution_id, locked=locked, check_sources=check_sources
    )
    expected = {
        "forward_operation_id": str(forward_execution.id),
        "forward_plan_id": str(original.id),
    }
    if (
        plan.content.get("selector") != expected
        or plan.content.get("before") != forward.desired.selected()
        or plan.content.get("desired") != forward.before.selected()
        or plan.content.get("config_references") != original.content["config_references"]
        or plan.workload_id != original.workload_id
        or plan.incident_id != original.incident_id
        or plan.template != forward.template
        or plan.target_epoch != forward.epoch
        or plan.expires_at > window.deadline
        or plan.content.get("verification_profile_digest")
        != original.content["rollback_contract"]["verification_profile_digest"]
        or window.inverse_execution_id
        not in (None, execution.id if execution is not None else None)
        or (
            execution is not None
            and plan.state == "approved"
            and window.inverse_execution_id != execution.id
        )
    ):
        deny("Inverse identity, original values, window or one-operation authority does not match.")
    return window


def propose(db, identifier, actor_id, rationale):
    from cloudops.features.recovery import plans, service

    with db.begin():
        original = db.get(Execution, identifier)
        if original is None:
            deny("The original forward execution is unavailable.")
        incident_id = db.get(ActionPlan, original.plan_id).incident_id
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if incident.state == "resolved":
            deny("Reopen the saved incident before proposing a separately approved inverse.")
        _, original_plan, window, forward = require_forward(db, identifier, locked=True)
        if window.inverse_execution_id is not None:
            deny(
                "This forward operation already has an approved inverse; inspect its independent outcome."
            )
        workload = require_active(db, original.workload_id)
        authority = service.available(db, workload)
        if authority["epoch"] != forward.epoch:
            deny("Maintenance retired the original forward authority.")
        kube = Kubernetes()
        deployment = kube.deployment(workload)
        kube.controllers(workload)
        now = database_now(db)
        expires = min(now + timedelta(seconds=300), window.deadline)
        content = deepcopy(original_plan.content)
        content.update(
            direction="inverse",
            origin="inverse_rollback",
            provenance={
                "origin": "inverse_rollback",
                "forward_operation_id": str(identifier),
                "forward_plan_id": str(original_plan.id),
                "forward_plan_digest": original_plan.digest,
                "quality": "Exact captured inverse; fresh independent approval and verification required.",
            },
            before=forward.desired.selected(),
            desired=forward.before.selected(),
            selector={
                "forward_operation_id": str(identifier),
                "forward_plan_id": str(original_plan.id),
            },
            observed_resource_version=deployment["metadata"]["resourceVersion"],
            expires_at=expires.isoformat(),
            rationale=rationale,
            rollback_contract=None,
            policy={"id": rules.id, "version": rules.version, "digest": rules.digest()}
            if (rules := policy.load())
            else None,
        )
        profile = load_for(workload.uid, forward.template, content["desired"])
        if profile is None or rules is None:
            deny("The original inverse policy or qualified verification profile is unavailable.")
        content.update(
            verification_profile=profile.model_dump(mode="json"),
            verification_profile_digest=profile.digest(),
        )
        content["diff"] = [
            {"field": k, "before": content["before"][k], "desired": content["desired"][k]}
            for k in content["before"]
            if content["before"][k] != content["desired"][k]
        ]
        if service.preflight(kube, deployment, content) != content["before"]:
            deny("Current fields no longer match the exact acknowledged forward desired state.")
        # No live observation may renew the original persisted rollback window.
        if database_now(db) >= expires:
            deny("The original rollback window expired during live preflight.")
        service.invalidate(db, incident_id)
        row = ActionPlan(
            incident_id=incident_id,
            workload_id=workload.id,
            template=forward.template,
            origin="inverse_rollback",
            forward_execution_id=identifier,
            content=content,
            digest=plans.digest(content),
            target_epoch=authority["epoch"],
            rationale=rationale,
            created_by=actor_id,
            expires_at=expires,
        )
        db.add(row)
        db.flush()
        require_inverse(db, row)
        return row
