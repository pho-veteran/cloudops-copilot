"""Deterministic selected-field previews over retained advice and live observations."""

from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select

from cloudops.features.diagnosis.advice import RCAResult
from cloudops.features.diagnosis.models import Analysis, AnalysisContext, AnalysisResult
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations import comparison_service
from cloudops.features.investigations.locators import digest, project
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.service import evidence_content
from cloudops.features.recovery import policy, service
from cloudops.features.recovery.fields import Ineligible, selected, safe_resources
from cloudops.features.recovery.baseline_context import supported_diff
from cloudops.features.recovery.models import ActionPlan
from cloudops.features.recovery.verification_policy import CAPABILITY, load_for
from cloudops.features.workloads.baseline_schemas import BaselineEnvelope
from cloudops.features.workloads.baseline_service import digest as baseline_digest, payload
from cloudops.features.workloads.kubernetes import Kubernetes, quantity
from cloudops.features.workloads.models import Baseline
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError


def manual(reason):
    return {
        "state": "manual_only",
        "reason": reason,
        "next_step": "Inspect fresh scoped evidence and qualified server policy; use a deployment operator for unsupported changes.",
    }


def qualify(content, rules):
    profile = load_for(content["workload"]["uid"], content["template"], content["desired"])
    content["direction"] = "forward"
    if profile is not None and rules.verification_policy == profile.id:
        content.update(
            execution_capability=CAPABILITY,
            verification_profile=profile.model_dump(mode="json"),
            verification_profile_digest=profile.digest(),
        )
        from cloudops.features.recovery.rollback import contract

        content["rollback_contract"] = contract(content, rules)
    return content


def manual_content(workload, deployment, replicas, rules, authority, rationale, now):
    if rules is None or workload.uid not in rules.workload_uids:
        return None
    try:
        containers, refs = selected(deployment)
    except Ineligible:
        return None
    before = {
        "uid": workload.uid,
        "replicas": deployment["spec"].get("replicas", 1),
        "containers": containers,
    }
    desired = {**before, "replicas": replicas}
    content = qualify(
        {
            "schema_version": "RecoveryPlan/v2",
            "origin": "operator_manual",
            "provenance": {
                "origin": "operator_manual",
                "quality": "Explicit manual scale; no RCA attribution.",
            },
            "workload": {
                key: getattr(workload, key) for key in ("cluster", "namespace", "name", "uid")
            },
            "template": "scale_deployment",
            "selector": {"replicas": replicas},
            "policy": {"id": rules.id, "version": rules.version, "digest": rules.digest()},
            "before": before,
            "desired": desired,
            "config_references": refs,
            "diff": [{"field": "replicas", "before": before["replicas"], "desired": replicas}],
            "target_epoch": authority["epoch"],
            "observed_resource_version": deployment["metadata"]["resourceVersion"],
            "expires_at": (now + timedelta(seconds=300)).isoformat(),
            "rationale": rationale,
            "verification_policy": rules.verification_policy,
            "inverse_policy": rules.inverse_policy,
            "execution_capability": "unavailable",
        },
        rules,
    )
    return content if content["execution_capability"] == CAPABILITY else None


def advice_source(db, incident, workload, body):
    analysis = db.get(Analysis, body.analysis_id)
    run = db.get(InvestigationRun, body.run_id)
    latest = db.scalar(
        select(InvestigationRun.id)
        .where(InvestigationRun.incident_id == incident.id)
        .order_by(InvestigationRun.created_at.desc(), InvestigationRun.id.desc())
        .limit(1)
    )
    stored = db.get(AnalysisResult, body.analysis_id)
    context = db.get(AnalysisContext, body.analysis_id)
    if not (
        analysis
        and run
        and stored
        and context
        and analysis.run_id == run.id == latest
        and run.incident_id == incident.id
        and run.state == "completed"
        and analysis.purpose == "operational"
        and analysis.state == "completed"
    ):
        raise Ineligible("Exact latest completed operational Run/Analysis is required.")
    result = RCAResult.model_validate(stored.payload)
    scope = context.summary.get("scope", {})
    expected = {
        "incident_id": str(incident.id),
        "run_id": str(run.id),
        "workload_id": str(workload.id),
        "workload_uid": workload.uid,
        **{key: getattr(workload, key) for key in ("cluster", "namespace", "name")},
    }
    if (
        any(scope.get(key) != value for key, value in expected.items())
        or result.run_id != run.id
        or result.incident_id != incident.id
        or result.context_digest != context.digest
        or context.summary.get("digest") != context.digest
        or context.summary.get("analysis_identity") != str(analysis.id)
        or not analysis.identity
        or result.status != "supported"
        or result.candidate_template_id != body.template
    ):
        raise Ineligible("Advice identity, candidate or retained context does not match.")
    references = result.supporting + result.contradicting
    for item in result.alternatives + result.ruled_out:
        references += item.evidence
    if not result.supporting:
        raise Ineligible("Supported retained evidence is required.")
    for reference in references:
        ref = reference.citation
        resolution = comparison_service.citation(
            db, incident.id, run.id, ref.evidence_id, ref.locator.kind, ref.locator.key
        )
        content = evidence_content(db, incident.id, run.id, ref.evidence_id)
        entries = project(content.payload_kind, content.payload_version, content.payload)
        entry = next(
            (
                entry
                for entry in entries
                if entry.kind == ref.locator.kind
                and entry.key == ref.locator.key
                and not entry.ambiguous
            ),
            None,
        )
        if (
            resolution.citation != ref
            or resolution.evidence.sha256 != reference.evidence_sha256
            or resolution.evidence.source_key != reference.source_key
            or resolution.evidence.coverage.status != reference.coverage_status
            or entry is None
            or digest(entry.value) != reference.entry_digest
        ):
            raise Ineligible("Retained evidence no longer resolves exactly.")
    return {
        "origin": "analysis_advice",
        "run_id": str(run.id),
        "analysis_id": str(analysis.id),
        "result_digest": digest(stored.payload),
        "context_digest": context.digest,
        "evidence": [ref.model_dump(mode="json") for ref in references],
        "quality": "Stored structural validation; advice does not authorize parameters or prove recovery.",
    }


def desired_fields(db, workload, body, rules, before, refs):
    desired = deepcopy(before)
    selector = {}
    if body.template == "restore_deployment_template":
        row = db.get(Baseline, body.baseline_id) if body.baseline_id else None
        if row is None or row.workload_id != workload.id:
            raise Ineligible("A confirmed baseline for the original target is required.")
        if row.snapshot.get("profile") != "deployment-images-resources/v1":
            raise Ineligible(
                "Legacy baseline provenance is unavailable; confirm a fresh E02 baseline."
            )
        saved = payload(row.snapshot)
        if (
            baseline_digest(saved) != row.digest
            or saved.provenance.second is None
            or any(
                getattr(saved.identity, key) != getattr(workload, key)
                for key in ("cluster", "namespace", "name", "uid")
            )
        ):
            raise Ineligible("Baseline provenance or original target does not match.")
        current = BaselineEnvelope(
            identity=saved.identity, containers=before["containers"], config_references=refs
        )
        changes = supported_diff(saved, current)
        if changes is None or saved.config_references != current.config_references:
            raise Ineligible(
                "Container set or external configuration reference drift is unsupported."
            )
        if not changes:
            raise Ineligible(
                "No supported restore diff; configuration-only changes require manual guidance."
            )
        desired["containers"] = sorted(
            [c.model_dump(mode="json", exclude_none=True) for c in saved.containers],
            key=lambda c: c["name"],
        )
        safe_resources(before["containers"], rules)
        safe_resources(desired["containers"], rules)
        if any(c["image"] not in rules.qualified_images for c in desired["containers"]):
            raise Ineligible("Restored images must be qualified by the server policy.")
        selector = {
            "baseline_id": str(row.id),
            "baseline_digest": row.digest,
            "baseline_profile": saved.profile,
        }
    elif body.template == "set_memory_profile":
        profile = next(
            (
                p
                for p in rules.memory_profiles
                if p.id == body.profile_id and p.workload_uid == workload.uid
            ),
            None,
        )
        if profile is None:
            raise Ineligible("Select an immutable qualified memory profile for this UID.")
        by_name = {c["name"]: c for c in desired["containers"]}
        if not set(profile.containers) <= by_name.keys():
            raise Ineligible("Configured memory containers are not present.")
        for name in profile.containers:
            memory = by_name[name]["resources"]
            if not (
                0 < quantity(memory["requests"].get("memory", 0)) <= quantity(profile.request)
                and 0 < quantity(memory["limits"].get("memory", 0)) < quantity(profile.limit)
            ):
                raise Ineligible("OOM mitigation requires a positive qualified memory increase.")
            memory["requests"]["memory"], memory["limits"]["memory"] = (
                profile.request,
                profile.limit,
            )
        safe_resources([by_name[name] for name in profile.containers], rules)
        selector = {
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": digest(profile.model_dump(mode="json")),
        }
    else:
        if body.replicas == before["replicas"]:
            raise Ineligible(
                "Target already has the desired replica count; no remediation is needed."
            )
        desired["replicas"] = body.replicas
        selector = {"replicas": body.replicas}
    return desired, selector


def propose(db, incident_id, actor_id, body):
    with db.begin():
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if not incident or incident.state == "resolved":
            raise DomainError(
                409, "incident_unavailable", "Reopen a saved incident before proposing a plan."
            )
        workload = require_active(db, incident.workload_id)
        try:
            rules = policy.load()
            if rules is None or workload.uid not in rules.workload_uids:
                return manual("Qualified recovery policy is unavailable for this target.")
            provenance = advice_source(db, incident, workload, body)
            authority = service.available(db, workload)
            kube = Kubernetes()
            deployment = kube.deployment(workload)
            kube.controllers(workload)
            containers, refs = selected(deployment)
            before = {
                "uid": workload.uid,
                "replicas": deployment["spec"].get("replicas", 1),
                "containers": containers,
            }
            desired, selector = desired_fields(db, workload, body, rules, before, refs)
            if body.template == "scale_deployment":
                kube.capacity(deployment, body.replicas)
        except Ineligible as error:
            return manual(str(error))
        except (ValueError, TypeError, KeyError, AttributeError, ObservationError, DomainError):
            return manual(
                "Exact advice, baseline/profile, live target or safe selected fields are unavailable or ineligible."
            )
        now = database_now(db)
        expires = now + timedelta(seconds=rules.plan_ttl_seconds)
        content = qualify(
            {
                "schema_version": "RecoveryPlan/v2",
                "origin": "analysis_advice",
                "provenance": provenance,
                "workload": {
                    key: getattr(workload, key) for key in ("cluster", "namespace", "name", "uid")
                },
                "template": body.template,
                "selector": selector,
                "policy": {"id": rules.id, "version": rules.version, "digest": rules.digest()},
                "before": before,
                "desired": desired,
                "config_references": refs,
                "diff": [
                    {"field": key, "before": before[key], "desired": desired[key]}
                    for key in before
                    if before[key] != desired[key]
                ],
                "target_epoch": authority["epoch"],
                "observed_resource_version": deployment["metadata"]["resourceVersion"],
                "expires_at": expires.isoformat(),
                "rationale": body.rationale,
                "prerequisites": [
                    "Original UID and relevant fields unchanged",
                    "Fresh controller and candidate/rollout capacity proof at dispatch",
                ],
                "verification_policy": rules.verification_policy,
                "inverse_policy": rules.inverse_policy,
                "execution_capability": "unavailable: matching gateway, lifecycle and qualified verification are required",
            },
            rules,
        )
        service.invalidate(db, incident_id)
        row = ActionPlan(
            incident_id=incident_id,
            workload_id=workload.id,
            template=body.template,
            origin="analysis_advice",
            source_analysis_id=body.analysis_id,
            content=content,
            digest=digest(content),
            target_epoch=authority["epoch"],
            rationale=body.rationale,
            created_by=actor_id,
            expires_at=expires,
        )
        db.add(row)
        db.flush()
        return row
