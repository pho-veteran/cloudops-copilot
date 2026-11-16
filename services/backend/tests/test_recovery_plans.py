"""Real PostgreSQL and authenticated API; Kubernetes and advice origins are controlled fixtures."""

import copy
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from diagnosis_fixtures import profile
from sqlalchemy import select, text
from test_diagnosis_validation import _crashloop, _raw, policy
from test_permissions import permission_context as _permission_context

from cloudops.features.diagnosis.advice import validate
from cloudops.features.diagnosis.context import build
from cloudops.features.diagnosis.models import Analysis, AnalysisContext, AnalysisResult
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.locators import digest
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.recovery import service
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.shared.jobs import Job
from cloudops.shared.database import BACKEND_ROOT, database_now, engine, sessions

permission_context = _permission_context


@pytest.fixture
def candidate(permission_context, tmp_path, monkeypatch):
    c = permission_context
    with sessions()() as db, db.begin():
        incident = db.get(Incident, UUID(c.consumer.incident_id))
        run = InvestigationRun(
            incident_id=incident.id,
            requested_by=incident.created_by,
            state="completed",
            question="Why is the workload restarting?",
            question_source="operator",
            window_start=database_now(db) - timedelta(minutes=30),
            window_end=database_now(db),
            window_source="explicit",
        )
        db.add(run)
        db.flush()
        run_id = run.id
        analysis = Analysis(
            id=uuid4(),
            run_id=run.id,
            state="completed",
            identity={"fixture": "controlled-no-inference"},
            dispatch_epoch={},
            deadline=database_now(db) + timedelta(minutes=15),
        )
        db.add(analysis)
        db.flush()
        analysis_id = analysis.id
        db.add(Job(kind="diagnosis_analysis_v1", subject_id=analysis.id, state="completed"))
    with sessions()() as db:
        scope = (UUID(c.consumer.incident_id), run_id, c.consumer.uid)
        _crashloop(db, scope)
        prepared = build(
            db,
            scope[0],
            scope[1],
            policy=policy(),
            profile=profile(),
            analysis_identity=str(analysis_id),
            selection=None,
        )
        outcome = validate(db, prepared, _raw())
        assert outcome.status == "valid", outcome
        db.add(
            AnalysisContext(
                analysis_id=analysis_id,
                digest=prepared.digest,
                snapshot=None,
                summary=prepared.record(),
            )
        )
        db.add(
            AnalysisResult(analysis_id=analysis_id, payload=outcome.result.model_dump(mode="json"))
        )
        db.commit()
    rules = {
        "schema_version": "RecoveryPolicy/v1",
        "id": "controlled-fixture",
        "version": "1",
        "workload_uids": [c.consumer.uid],
        "max_cpu": "1",
        "max_memory": "512Mi",
        "qualified_images": ["example/fixture:1"],
        "memory_profiles": [
            {
                "id": "fixture-memory",
                "version": "1",
                "workload_uid": c.consumer.uid,
                "containers": ["consumer"],
                "request": "64Mi",
                "limit": "128Mi",
                "qualification_reference": "test://fixture-only",
            }
        ],
        "verification_policy": "fixture-unqualified/v1",
        "inverse_policy": "fixture-deny/v1",
    }
    path = tmp_path / "recovery.json"
    path.write_text(json.dumps(rules))
    monkeypatch.setenv("RECOVERY_POLICY_FILE", str(path))
    c.recovery_body = {
        "origin": "analysis_advice",
        "run_id": str(run_id),
        "analysis_id": str(analysis_id),
        "template": "restore_deployment_template",
        "baseline_id": c.consumer.baseline_id,
        "rationale": "Controlled applicability test; no live inference or writes.",
    }
    c.recovery_rules, c.recovery_path = rules, path
    return c


def propose(c, **changes):
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/plans",
        json={**c.recovery_body, **changes},
        headers=actor.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def live_baseline(c):
    from test_baselines_v1 import confirm, preview

    response = confirm(c, preview(c))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_restore_exact_provenance_diff_and_preview_never_dispatches(candidate):
    c = candidate
    c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
        "cpu": "100m",
        "memory": "64Mi",
    }
    baseline = live_baseline(c)
    c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "example/fixture:broken"
    )
    value = propose(c, baseline_id=baseline)
    assert value["template"] == "restore_deployment_template"
    content = value["content"]
    assert content["before"]["containers"][0]["image"].endswith(":broken")
    assert content["desired"]["containers"][0]["image"].endswith(":1")
    assert value["digest"] == digest(content)
    assert content["provenance"]["run_id"] == c.recovery_body["run_id"]
    assert content["selector"]["baseline_id"] == baseline
    assert content["provenance"]["evidence"]
    actor = c.actors["operator"]
    reviewed = actor.client.get(f"/api/plans/{value['id']}").json()
    assert not reviewed["valid"] and "Preview only" in reviewed["reason"]
    for role in ("operator", "viewer"):
        assert (
            c.actors[role].client.get(f"/api/plans/{value['id']}").json()["plan"]["content"]
            == content
        )
    response = actor.client.post(
        f"/api/plans/{value['id']}/decision",
        headers=actor.headers,
        json={
            "version": value["version"],
            "digest": value["digest"],
            "review_token": reviewed["review_token"],
            "decision": "approve",
        },
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "execution_unavailable"
    )
    with sessions()() as db:
        assert db.scalar(select(Execution).where(Execution.plan_id == UUID(value["id"]))) is None
        assert db.get(ActionPlan, UUID(value["id"])).content == content


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "no_diff",
        "config",
        "missing_resources",
        "container_set",
        "unqualified_image",
        "missing",
    ],
)
def test_restore_manual_only_reasons(candidate, change):
    c = candidate
    container = c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]
    container["resources"]["limits"] = {"cpu": "100m", "memory": "64Mi"}
    baseline = c.consumer.baseline_id if change == "legacy" else live_baseline(c)
    if change != "no_diff":
        container["image"] = "example/fixture:broken"
    if change == "config":
        container["envFrom"] = [{"configMapRef": {"name": "changed"}}]
    if change == "missing_resources":
        del container["resources"]["requests"]["memory"]
    if change == "container_set":
        container["name"] = "replacement"
    if change == "unqualified_image":
        c.recovery_rules["qualified_images"] = []
        c.recovery_path.write_text(json.dumps(c.recovery_rules))
    value = propose(c, baseline_id=str(uuid4()) if change == "missing" else baseline)
    assert value["state"] == "manual_only" and value["reason"] and value["next_step"]


def memory_advice(c):
    with sessions()() as db, db.begin():
        row = db.get(AnalysisResult, UUID(c.recovery_body["analysis_id"]))
        result = copy.deepcopy(row.payload)
        result.update(candidate_template_id="set_memory_profile", cause_category="oom_restarts")
        row.payload = result
    container = c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]
    container["resources"]["limits"] = {"cpu": "100m", "memory": "64Mi"}
    c.recovery_body.pop("baseline_id")
    c.recovery_body.update(template="set_memory_profile", profile_id="fixture-memory")


def test_memory_server_values_preserve_cpu_and_image_and_policy_change_needs_new_plan(candidate):
    c = candidate
    memory_advice(c)
    value = propose(c)
    old, new = (value["content"][key]["containers"][0] for key in ("before", "desired"))
    assert old["image"] == new["image"]
    assert old["resources"]["requests"]["cpu"] == new["resources"]["requests"]["cpu"]
    assert new["resources"]["limits"]["memory"] == "128Mi"
    c.recovery_rules["version"] = "2"
    c.recovery_rules["memory_profiles"][0]["version"] = "2"
    c.recovery_path.write_text(json.dumps(c.recovery_rules))
    newer = propose(c)
    assert newer["id"] != value["id"] and newer["content"]["policy"] != value["content"]["policy"]
    assert (
        c.actors["viewer"].client.get(f"/api/plans/{value['id']}").json()["plan"]["content"]
        == value["content"]
    )
    assert propose(c, profile_id="arbitrary")["state"] == "manual_only"
    assert propose(c, profile_id=None)["state"] == "manual_only"


@pytest.mark.parametrize(
    "change",
    [
        "foreign_run",
        "foreign_analysis",
        "old_run",
        "target",
        "context",
        "evidence",
        "expired_evidence",
        "evaluation",
        "unsupported",
    ],
)
def test_source_identity_or_evidence_changes_cannot_produce_plan(candidate, change):
    c = candidate
    if change in ("foreign_run", "foreign_analysis"):
        c.recovery_body["run_id" if change == "foreign_run" else "analysis_id"] = str(uuid4())
    with sessions()() as db, db.begin():
        if change == "old_run":
            incident = db.get(Incident, UUID(c.consumer.incident_id))
            db.add(
                InvestigationRun(
                    incident_id=incident.id, requested_by=incident.created_by, state="failed"
                )
            )
        elif change == "target":
            db.get(Workload, UUID(c.consumer.workload_id)).uid = "replacement-uid"
        elif change == "context":
            db.get(AnalysisContext, UUID(c.recovery_body["analysis_id"])).digest = "f" * 64
        elif change in ("evidence", "expired_evidence"):
            rows = db.scalars(
                select(Evidence).where(Evidence.run_id == UUID(c.recovery_body["run_id"]))
            ).all()
            for row in rows:
                if change == "evidence":
                    row.sha256 = "f" * 64
                else:
                    row.expires_at = database_now(db) - timedelta(seconds=1)
        elif change == "evaluation":
            db.get(Analysis, UUID(c.recovery_body["analysis_id"])).purpose = "evaluation"
        elif change == "unsupported":
            row = db.get(AnalysisResult, UUID(c.recovery_body["analysis_id"]))
            row.payload = {**row.payload, "candidate_template_id": None}
    assert propose(c)["state"] == "manual_only"


def test_analysis_scale_origin_is_distinct_from_manual_scale(candidate):
    c = candidate
    with sessions()() as db, db.begin():
        row = db.get(AnalysisResult, UUID(c.recovery_body["analysis_id"]))
        row.payload = {
            **row.payload,
            "candidate_template_id": "scale_deployment",
            "cause_category": "traffic_latency_errors",
        }
    c.recovery_body.pop("baseline_id")
    value = propose(c, template="scale_deployment", replicas=2)
    assert value["origin"] == "analysis_advice"
    assert value["content"]["desired"]["replicas"] == 2
    assert value["content"]["provenance"]["analysis_id"] == c.recovery_body["analysis_id"]
    assert propose(c, template="scale_deployment", replicas=1)["state"] == "manual_only"


@pytest.mark.parametrize(
    "change",
    ["missing", "bad_json", "bad_quantity", "duplicate_profile", "excess_memory", "unknown_field"],
)
def test_invalid_server_policy_is_manual_only(candidate, change):
    c = candidate
    if change == "missing":
        c.recovery_path.unlink()
    elif change == "bad_json":
        c.recovery_path.write_text("{")
    else:
        if change == "bad_quantity":
            c.recovery_rules["max_memory"] = "not-a-quantity"
        if change == "duplicate_profile":
            c.recovery_rules["memory_profiles"] *= 2
        if change == "excess_memory":
            c.recovery_rules["memory_profiles"][0]["limit"] = "1Gi"
        if change == "unknown_field":
            c.recovery_rules["arbitrary_patch"] = []
        c.recovery_path.write_text(json.dumps(c.recovery_rules))
    assert propose(c)["state"] == "manual_only"
    assert c.actors["viewer"].client.get("/api/recovery-policy").json()["policy"] is None


def test_closed_selectors_policy_roles_and_manual_scale(candidate):
    c = candidate
    path = f"/api/incidents/{c.consumer.incident_id}/plans"
    for role, status in (("anonymous", 401), ("viewer", 403)):
        actor = c.actors[role]
        assert (
            actor.client.post(path, headers=actor.headers, json=c.recovery_body).status_code
            == status
        )
    actor = c.actors["operator"]
    assert (
        actor.client.post(
            path, headers=actor.headers, json={**c.recovery_body, "memory_limit": "1Gi"}
        ).status_code
        == 422
    )
    assert (
        actor.client.post(
            path, headers=actor.headers, json={**c.recovery_body, "profile_id": "arbitrary"}
        ).status_code
        == 422
    )
    rules = c.actors["viewer"].client.get("/api/recovery-policy").json()
    assert rules["policy"]["memory_profiles"][0]["id"] == "fixture-memory"
    c.recovery_path.unlink()
    assert propose(c)["state"] == "manual_only"
    response = actor.client.post(
        path,
        headers=actor.headers,
        json={"replicas": 2, "rationale": "Explicit manual scale; no diagnosis claimed."},
    )
    assert response.status_code == 201 and response.json()["origin"] == "operator_manual"


def test_status_rv_movement_keeps_exact_diff_and_freezes_dispatch_preconditions(
    permission_context, monkeypatch
):
    c = permission_context
    actor = c.actors["operator"]
    original = actor.client.get(f"/api/plans/{c.consumer.plan_id}").json()
    c.consumer.deployment["metadata"]["resourceVersion"] = "new-status-only-rv"
    reviewed = actor.client.get(f"/api/plans/{c.consumer.plan_id}").json()
    assert reviewed["valid"] and reviewed["plan"]["content"] == original["plan"]["content"]
    response = actor.client.post(
        f"/api/plans/{c.consumer.plan_id}/decision",
        headers=actor.headers,
        json={
            "version": 1,
            "digest": reviewed["plan"]["digest"],
            "review_token": reviewed["review_token"],
            "decision": "approve",
        },
    )
    assert response.status_code == 200, response.text
    with sessions()() as db:
        execution = db.scalar(
            select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id))
        )
        identifier = execution.id
    requests = []
    monkeypatch.setattr(
        service.client,
        "gateway",
        lambda path, value: requests.append(value) or {"outcome": "acknowledged"},
    )
    service.execute(identifier, before_call=lambda: None)
    with sessions()() as db, db.begin():
        service.ExecutionHandler().failed(db, identifier, "Controlled lost worker", False)
    service.execute(identifier, before_call=lambda: None)
    with sessions()() as db, db.begin():
        service.ExecutionHandler().publish(
            db, identifier, {"outcome": "acknowledged", "verification": "inconclusive"}
        )
    with sessions()() as db:
        assert (
            db.get(Execution, identifier).result["dispatch_resource_version"]
            == "new-status-only-rv"
        )
    service.execute(identifier, before_call=lambda: None)
    c.consumer.deployment["metadata"]["resourceVersion"] = "later-status-rv"
    service.execute(identifier, before_call=lambda: None)
    assert all(request == requests[0] for request in requests)
    assert requests[0]["resource_version"] == "new-status-only-rv"


def test_provenance_migration_preserves_legacy_content_digest_and_saved_history(permission_context):
    c = permission_context
    actor = c.actors["operator"]
    path = f"/api/incidents/{c.consumer.incident_id}"
    resolved = actor.client.post(
        path + "/lifecycle",
        headers=actor.headers,
        json={
            "action": "resolve",
            "reason": "Preserve saved handoff through the provenance migration.",
        },
    )
    assert resolved.status_code == 200, resolved.text
    saved = actor.client.get(path + "/handoff").content
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with engine().connect() as db:
        before = db.execute(
            text("SELECT id,content,digest,state FROM action_plans ORDER BY id")
        ).all()
    command.downgrade(config, "0012_diagnosis_lifecycle")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine().connect() as db:
        assert (
            db.execute(text("SELECT id,content,digest,state FROM action_plans ORDER BY id")).all()
            == before
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM action_plans WHERE origin IS NOT NULL OR source_analysis_id IS NOT NULL"
                )
            ).scalar()
            == 0
        )
    assert (
        c.actors["viewer"].client.get(f"/api/plans/{c.consumer.plan_id}").json()["plan"]["origin"]
        is None
    )
    assert actor.client.get(path + "/handoff").content == saved


def test_downgrade_refuses_to_remove_retained_v2_provenance(candidate):
    c = candidate
    memory_advice(c)
    value = propose(c)
    with pytest.raises(RuntimeError, match="Retained v2 plans"):
        command.downgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "0012_diagnosis_lifecycle")
    retained = c.actors["viewer"].client.get(f"/api/plans/{value['id']}").json()["plan"]
    assert retained["content"] == value["content"] and retained["digest"] == value["digest"]
