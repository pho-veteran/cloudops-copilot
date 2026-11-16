"""Read-only target authority, separate from retained operation outcomes and health."""

from sqlalchemy import select

from cloudops.features.recovery import client, policy, service
from cloudops.features.recovery.classification import BLOCKING, classification_expression
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.verification_policy import configured_profiles
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError


def read(db, workload_id):
    workload = db.get(Workload, workload_id)
    if workload is None:
        raise DomainError(404, "workload_not_found", "Workload not found.")
    rows = db.execute(
        select(Execution, ActionPlan.incident_id)
        .join(ActionPlan, Execution.plan_id == ActionPlan.id)
        .where(
            Execution.workload_id == workload.id,
            Execution.holds_lock
            | classification_expression(
                Execution.state,
                Execution.holds_lock,
                Execution.maintenance_released,
                Execution.result,
            ).in_(BLOCKING),
        )
        .order_by(Execution.created_at.desc(), Execution.id.desc())
        .limit(26)
    ).all()
    recent = db.execute(
        select(Execution, ActionPlan.incident_id)
        .join(ActionPlan, Execution.plan_id == ActionPlan.id)
        .where(Execution.workload_id == workload.id)
        .order_by(Execution.created_at.desc(), Execution.id.desc())
        .limit(5)
    ).all()
    result = {
        "workload_id": workload.id,
        "uid": workload.uid,
        "observed_at": database_now(db),
        "target_authority": "unavailable",
        "application_state": "unavailable",
        "epoch": None,
        "reason": "An active configured live target is required. Historical evidence grants no write authority.",
        "qualified_templates": [],
        "blocking_executions": [
            {
                "id": row.id,
                "incident_id": incident,
                "state": row.state,
                "holds_lock": row.holds_lock,
                "outcome": row.outcome,
            }
            for row, incident in rows[:25]
        ],
        "blocking_executions_truncated": len(rows) > 25,
        "recent_executions": [
            {
                "id": row.id,
                "incident_id": incident,
                "state": row.state,
                "holds_lock": row.holds_lock,
                "outcome": row.outcome,
            }
            for row, incident in recent
        ],
    }
    if not workload.active or workload.source != "live_kubernetes":
        return result
    rules = policy.load()
    if rules and workload.uid in rules.workload_uids:
        result["qualified_templates"] = sorted(
            {
                profile.template
                for profile in configured_profiles()
                if profile.workload_uid == workload.uid and profile.id == rules.verification_policy
            }
        )
    try:
        authority = client.target(workload.uid)
        if (
            authority.get("uid") != workload.uid
            or not isinstance(authority.get("epoch"), str)
            or not authority["epoch"]
            or type(authority.get("enabled")) is not bool
            or type(authority.get("recovery_mode")) is not bool
        ):
            raise DomainError(
                503, "executor_unavailable", "Exact current target authority is unavailable."
            )
        result["epoch"] = authority["epoch"]
        result["target_authority"] = (
            "enabled" if authority["enabled"] and not authority["recovery_mode"] else "quarantined"
        )
        if result["target_authority"] == "quarantined":
            result["reason"] = (
                "The isolated target is quarantined. Trusted deployment maintenance must establish safety; application roles cannot unlock it."
            )
        else:
            result["application_state"] = "ready_for_preflight"
            result["reason"] = (
                "Current target authority permits a fresh preflight, not a write. Exact live checks and explicit approval remain required."
            )
        try:
            service._available_locally(db, workload)
        except DomainError as error:
            result["application_state"] = "blocked"
            result["reason"] = error.message + " " + result["reason"]
    except (DomainError, TypeError, AttributeError) as error:
        result["reason"] = (
            error.message
            if isinstance(error, DomainError)
            else "Exact current target authority is unavailable."
        )
    return result
