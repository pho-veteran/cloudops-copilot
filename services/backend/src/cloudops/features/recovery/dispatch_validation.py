"""Independent gateway admission checks; inverse authority comes from the durable forward receipt."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from cloudops.features.investigations.locators import digest
from cloudops.features.incidents.models import Incident
from cloudops.features.recovery.dispatch import RecoveryDispatch
from cloudops.features.recovery.fields import safe_resources, selected
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.policy import load
from cloudops.features.workloads.baseline_service import digest as baseline_digest, payload
from cloudops.features.workloads.kubernetes import quantity
from cloudops.features.workloads.models import Baseline, Workload
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError


def deny(message, code="intent_ineligible"):
    raise DomainError(409, code, message)


def approved_plan(body, workload, rules):
    try:
        with sessions()() as db:
            row = db.get(ActionPlan, body.plan_id)
            execution = db.get(Execution, body.execution_id)
            if (
                row is None
                or row.state != "approved"
                or row.decided_by is None
                or row.decided_at is None
            ):
                deny("An approved exact plan is required.")
            target = db.get(Workload, row.workload_id)
            incident = db.get(Incident, row.incident_id)
            if (
                target is None
                or not target.active
                or target.source != "live_kubernetes"
                or incident is None
                or incident.state == "resolved"
                or any(
                    getattr(target, key) != getattr(workload, key)
                    for key in ("cluster", "namespace", "name", "uid")
                )
            ):
                deny("The plan's original active workload and incident are unavailable.")
            if row.expires_at > datetime.now(timezone.utc) + timedelta(
                seconds=rules.plan_ttl_seconds
            ):
                deny("Plan expiry exceeds the qualified policy TTL.")
            if (
                execution is None
                or execution.plan_id != row.id
                or execution.workload_id != row.workload_id
                or execution.state not in ("executing", "rolling_back")
                or not execution.holds_lock
                or execution.maintenance_released
            ):
                deny("The exact active claimed execution is required.")
            content = row.content
            if body.direction == "inverse":
                from cloudops.features.recovery.rollback import require_inverse

                # The gateway rechecks captured safety/window authority without observer credentials.
                require_inverse(db, row, execution, check_sources=False)
            if (
                row.version != body.plan_version
                or row.digest != body.plan_digest
                or digest(content) != row.digest
                or row.template != body.template
                or row.target_epoch != body.epoch
                or row.expires_at != body.expires_at
                or datetime.fromisoformat(content.get("expires_at", "")) != body.expires_at
                or content.get("schema_version") != "RecoveryPlan/v2"
                or content.get("execution_capability") != "conditional_dispatch_v2"
                or content.get("direction", "forward") != body.direction
                or content.get("before") != body.before.selected()
                or content.get("desired") != body.desired.selected()
                or content.get("selector") != body.selector.model_dump(mode="json")
                or content.get("policy") != body.policy.model_dump(mode="json")
                or any(
                    content.get("workload", {}).get(key) != getattr(workload, key)
                    for key in ("cluster", "namespace", "name", "uid")
                )
            ):
                deny("Approved plan content, identity or execution capability does not match.")
    except SQLAlchemyError:
        deny("Approved plan storage is unavailable.", "plan_unavailable")


def restore_baseline(body, workload, references):
    try:
        with sessions()() as db:
            row = db.get(Baseline, body.selector.baseline_id)
            if row is None or row.digest != body.selector.baseline_digest:
                deny("The exact confirmed baseline is unavailable.")
            saved = payload(row.snapshot)
            if (
                baseline_digest(saved) != row.digest
                or saved.provenance.second is None
                or saved.profile != body.selector.baseline_profile
                or any(
                    getattr(saved.identity, key) != getattr(workload, key)
                    for key in ("cluster", "namespace", "name", "uid")
                )
            ):
                deny("Baseline provenance or original target does not match.")
            containers = sorted(
                [c.model_dump(mode="json", exclude_none=True) for c in saved.containers],
                key=lambda c: c["name"],
            )
            refs = sorted(
                [r.model_dump(mode="json", exclude_none=True) for r in saved.config_references],
                key=lambda r: (r["path"], digest(r)),
            )
            if containers != body.desired.selected()["containers"] or references != refs:
                deny("Restore differs from the selected baseline or configuration references.")
    except SQLAlchemyError:
        deny("Confirmed baseline storage is unavailable.", "baseline_unavailable")


def field_scope(body):
    before, desired = body.before.selected(), body.desired.selected()
    if desired == before:
        deny("No functional selected-field change is needed.", "no_op")
    if desired["replicas"] < 1:
        deny("A serving replica count is required.")
    if body.template == "scale_deployment":
        if not 1 <= desired["replicas"] <= 3 or before["containers"] != desired["containers"]:
            deny("Scale may change only the bounded absolute replica count.")
    elif before["replicas"] != desired["replicas"]:
        deny("Template and memory operations cannot change replicas.")
    if body.template == "set_memory_profile":
        for old, new in zip(before["containers"], desired["containers"], strict=True):
            if old["image"] != new["image"] or any(
                old["resources"][group].get("cpu") != new["resources"][group].get("cpu")
                for group in ("requests", "limits")
            ):
                deny("Memory operations cannot change image or CPU.")


def inverse_values(body, journal, rules):
    selector = body.selector
    row = journal.execute(
        "SELECT request,state FROM attempts WHERE id=?", (str(selector.forward_operation_id),)
    ).fetchone()
    if row is None or row["state"] != "acknowledged":
        deny("Only a durable acknowledged forward operation can authorize an inverse.")
    import json

    forward = RecoveryDispatch.model_validate(json.loads(row["request"]))
    if (
        forward.direction != "forward"
        or forward.plan_id != selector.forward_plan_id
        or forward.uid != body.uid
        or forward.template != body.template
        or body.before.selected() != forward.desired.selected()
        or body.desired.selected() != forward.before.selected()
    ):
        deny("Inverse values must match the acknowledged forward operation exactly.")
    if body.template != "scale_deployment":
        eligible = any(
            item.workload_uid == body.uid
            and sorted(
                [c.model_dump(mode="json", exclude_none=True) for c in item.containers],
                key=lambda c: c["name"],
            )
            == body.desired.selected()["containers"]
            for item in rules.inverse_qualified_states
        )
        if not eligible:
            deny("Captured old template/resources lack independent inverse qualification.")
    prior = journal.execute(
        "SELECT operation_id FROM inverse_links WHERE forward_id=?",
        (str(selector.forward_operation_id),),
    ).fetchone()
    if prior is not None and prior["operation_id"] != str(body.operation_id):
        deny("An inverse was already admitted for this forward operation.")


def validate_intent(body, workload, current, journal):
    if body.expires_at <= datetime.now(timezone.utc):
        deny("The plan expired before gateway admission.", "plan_expired")
    rules = load()
    if (
        rules is None
        or body.uid not in rules.workload_uids
        or body.policy.model_dump()
        != {"id": rules.id, "version": rules.version, "digest": rules.digest()}
    ):
        deny("Exact qualified recovery policy is unavailable.")
    approved_plan(body, workload, rules)
    containers, references = selected(current)
    observed = {
        "uid": current["metadata"]["uid"],
        "replicas": current["spec"].get("replicas", 1),
        "containers": containers,
    }
    if (
        observed != body.before.selected()
        or current["metadata"]["resourceVersion"] != body.resource_version
    ):
        deny(
            "Original UID, captured resourceVersion or approved relevant fields changed.",
            "target_changed",
        )
    field_scope(body)
    safe_resources(body.before.selected()["containers"], rules)
    safe_resources(body.desired.selected()["containers"], rules)
    if body.direction == "inverse":
        inverse_values(body, journal, rules)
    elif body.template == "restore_deployment_template":
        restore_baseline(body, workload, references)
    elif body.template == "set_memory_profile":
        profile = next(
            (
                p
                for p in rules.memory_profiles
                if p.id == body.selector.profile_id and p.workload_uid == body.uid
            ),
            None,
        )
        if (
            profile is None
            or profile.version != body.selector.profile_version
            or digest(profile.model_dump(mode="json")) != body.selector.profile_digest
        ):
            deny("Exact named memory profile is unavailable.")
        old = {c["name"]: c for c in body.before.selected()["containers"]}
        new = {c["name"]: c for c in body.desired.selected()["containers"]}
        if not set(profile.containers) <= old.keys():
            deny("Configured memory containers are missing.")
        for name in old:
            if name not in profile.containers:
                if old[name] != new[name]:
                    deny("Memory changes are limited to configured container names.")
                continue
            if any(
                new[name]["resources"][group]["memory"] != value
                for group, value in (("requests", profile.request), ("limits", profile.limit))
            ):
                deny("Memory values must match the immutable named profile.")
            if not (
                0
                < quantity(old[name]["resources"]["requests"]["memory"])
                <= quantity(profile.request)
                and quantity(old[name]["resources"]["limits"]["memory"]) < quantity(profile.limit)
            ):
                deny("Forward OOM mitigation requires a qualified memory increase.")
    elif body.selector.replicas != body.desired.replicas:
        deny("Scale desired replicas do not match the approved selector.")
    if body.template == "restore_deployment_template" and any(
        c["image"] not in rules.qualified_images for c in body.desired.selected()["containers"]
    ):
        deny("Desired images are not independently qualified.")
