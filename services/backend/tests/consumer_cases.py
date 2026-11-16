"""Executable consumer receipts with real PostgreSQL/auth and explicitly controlled provider observations."""

import copy
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from permission_cases import RequestCase
from sqlalchemy import select

from cloudops.features.access.service import token_hash
from cloudops.features.connections.models import ConnectionCheck
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recovery.api import PolicyView, RecoveryAvailability, ExecutionView
from cloudops.features.recovery import client as gateway_client
from cloudops.features.recovery.models import (
    ActionPlan,
    Execution,
    PlanReview,
    Verification,
    RollbackWindow,
)
from cloudops.features.workloads.kubernetes import Kubernetes, snapshot
from cloudops.features.workloads.models import (
    Baseline,
    BaselineCandidate,
    ScopeState,
    ScopeTarget,
    Workload,
)
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job
from cloudops.features.workloads import baseline_service
from cloudops.features.recovery import baseline_context
from cloudops.features.workloads.baseline_schemas import BaselinePayload, Provenance
from cloudops.features.workloads.observation_schemas import DirectOutcome, Identity
from cloudops.features.workloads.observation_service import public
from observation_fixtures import objects, observed


def consumer_observation(workload, deployment, at=None):
    data = objects()
    dep, replicasets, pods = data
    dep.clear()
    dep.update(copy.deepcopy(deployment))
    dep.update(kind="Deployment", apiVersion="apps/v1")
    dep["metadata"].update(name=workload.name, namespace=workload.namespace)
    rs = replicasets["items"][0]
    rs["metadata"]["ownerReferences"][0]["uid"] = workload.uid
    rs["spec"]["template"] = copy.deepcopy(dep["spec"]["template"])
    pods["items"][0]["spec"] = copy.deepcopy(dep["spec"]["template"]["spec"])
    pods["items"][0]["status"]["containerStatuses"][0]["name"] = dep["spec"]["template"]["spec"][
        "containers"
    ][0]["name"]
    return observed(
        Identity(
            workload_id=workload.id,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
            uid=workload.uid,
            epoch=workload.observation_epoch,
        ),
        at,
        data,
    )


CONSUMER_MODELS = (
    ScopeState,
    ScopeTarget,
    Workload,
    BaselineCandidate,
    Baseline,
    IncidentActivity,
    RecommendationReport,
    ConnectionCheck,
    ActionPlan,
    Execution,
    PlanReview,
    Verification,
    RollbackWindow,
)


def seed(context, monkeypatch):
    with sessions()() as db, db.begin():
        now = database_now(db)
        actor_id = UUID(context.actors["operator"].session["principal"]["id"])
        workload = Workload(
            cluster="permission-provider-fixture",
            namespace="demo",
            name="consumer",
            uid=str(uuid4()),
            source="live_kubernetes",
        )
        db.add(workload)
        db.flush()
        deployment = {
            "metadata": {"uid": workload.uid, "resourceVersion": "fixture-rv", "generation": 1},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "consumer",
                                "image": "example/fixture:1",
                                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                            }
                        ]
                    }
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        }
        value = snapshot(deployment)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        old = BaselineCandidate(
            workload_id=workload.id,
            digest=digest,
            snapshot=value,
            created_at=now - timedelta(minutes=1),
        )
        candidate = BaselineCandidate(
            workload_id=workload.id,
            digest=digest,
            snapshot=value,
            created_at=now - timedelta(seconds=10),
        )
        observation = consumer_observation(workload, deployment, now - timedelta(seconds=10))
        v1 = BaselinePayload(
            **baseline_service.envelope(observation).model_dump(),
            provenance=Provenance(first=public(observation)),
        )
        candidate.snapshot = {
            **v1.model_dump(mode="json", exclude_none=True),
            "_session_binding": token_hash(context.actors["operator"].cookie),
        }
        candidate.digest = baseline_service.digest(v1)
        incident = Incident(
            workload_id=workload.id,
            title="Consumer permission fixture",
            description="Explicit provider fixture; not a live cluster receipt.",
            created_by=actor_id,
        )
        report = RecommendationReport(
            workload_id=workload.id,
            state="insufficient_data",
            created_by=actor_id,
            result={
                "source": "controlled_provider_fixture",
                "coverage": None,
                "recommendations": None,
            },
            reason="Representative history is unqualified.",
        )
        check = ConnectionCheck(
            component="database",
            status="available",
            source="runtime",
            reason="Persisted prior connection check.",
            created_by=actor_id,
        )
        db.add_all([old, candidate, incident, report, check])
        db.flush()
        baseline = Baseline(
            workload_id=workload.id,
            candidate_id=old.id,
            digest=digest,
            snapshot=value,
            confirmed_by=actor_id,
        )
        note = IncidentActivity(
            incident_id=incident.id, kind="note", text="Saved consumer note", actor_id=actor_id
        )
        content = {
            "source": "controlled_provider_fixture",
            "workload": {"uid": workload.uid},
            "before": value,
            "resource_version": "fixture-rv",
            "replicas": 2,
        }
        plan_digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        plan = ActionPlan(
            incident_id=incident.id,
            workload_id=workload.id,
            content=content,
            digest=plan_digest,
            target_epoch="fixture-epoch",
            rationale="Permission request fixture",
            created_by=actor_id,
            expires_at=now + timedelta(minutes=5),
        )
        historical = ActionPlan(
            incident_id=incident.id,
            workload_id=workload.id,
            content=content,
            digest=plan_digest,
            target_epoch="old-fixture-epoch",
            state="approved",
            rationale="Prior fixture plan",
            created_by=actor_id,
            expires_at=now - timedelta(minutes=5),
        )
        db.add_all([baseline, note, plan, historical])
        db.flush()
        # A separate incident holds one deferred request so cancellation has a real target.
        pending_incident = Incident(
            workload_id=workload.id,
            title="Deferred permission fixture",
            description="Waiting for dispatch capacity; no collection was attempted.",
            created_by=actor_id,
        )
        db.add(pending_incident)
        db.flush()
        pending = InvestigationRun(
            incident_id=pending_incident.id,
            requested_by=actor_id,
            question="Pending fixture question",
            question_source="operator",
            window_start=now - timedelta(minutes=30),
            window_end=now,
            window_source="preset_30",
            queue_reason="capacity",
            reason="Accepted; waiting for dispatch capacity. It starts automatically.",
        )
        db.add(pending)
        db.flush()
        execution = Execution(
            created_at=now - timedelta(minutes=1),
            plan_id=historical.id,
            workload_id=workload.id,
            state="manual_intervention",
            holds_lock=False,
            result={
                "verification": "inconclusive",
                "reason": "Persisted synthetic execution receipt.",
            },
        )
        review_token = "operator-review-fixture-token"
        db.add(
            PlanReview(
                plan_id=plan.id,
                token_hash=token_hash(review_token),
                session_binding=token_hash(context.actors["operator"].session["csrf_token"]),
                expires_at=now + timedelta(seconds=60),
            )
        )
        db.add(execution)
        db.flush()
        context.consumer = SimpleNamespace(
            workload_id=str(workload.id),
            uid=workload.uid,
            incident_id=str(incident.id),
            candidate_id=str(candidate.id),
            digest=candidate.digest,
            baseline_id=str(baseline.id),
            report_id=str(report.id),
            plan_id=str(plan.id),
            plan_digest=plan_digest,
            review_token=review_token,
            execution_id=str(execution.id),
            deployment=deployment,
            pending_incident_id=str(pending_incident.id),
            pending_run_id=str(pending.id),
        )
        live_workload = SimpleNamespace(
            id=workload.id,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
            uid=workload.uid,
            observation_epoch=workload.observation_epoch,
        )

    async def direct(workload_id, purpose):
        with sessions()() as db:
            now = database_now(db)
        value = consumer_observation(live_workload, deployment, now)
        return DirectOutcome(outcome="complete", observation=value)

    monkeypatch.setattr(baseline_service, "observe_direct", direct)
    monkeypatch.setattr(baseline_context, "observe_direct", direct)
    monkeypatch.setattr(Kubernetes, "__init__", lambda self, path=None: None)
    monkeypatch.setattr(Kubernetes, "deployment", lambda self, workload: copy.deepcopy(deployment))
    monkeypatch.setattr(Kubernetes, "controllers", lambda *_args: None)
    monkeypatch.setattr(Kubernetes, "capacity", lambda *_args: None)
    monkeypatch.setattr(
        gateway_client,
        "target",
        lambda uid: {"uid": uid, "epoch": "fixture-epoch", "enabled": True, "recovery_mode": False},
    )


def workload_path(c):
    return {"path_params": {"workload_id": c.consumer.workload_id}}


def incident_path(c):
    return {"path_params": {"incident_id": c.consumer.incident_id}}


def report_path(c):
    return {"path_params": {"report_id": c.consumer.report_id}}


def plan_path(c):
    return {"path_params": {"plan_id": c.consumer.plan_id}}


def saved_list(response, c, actor, field, value):
    assert response.status_code == 200, response.text
    assert any(item[field] == value for item in response.json())


def candidate_check(response, c, actor):
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["workload_id"] == c.consumer.workload_id
    assert (
        value["digest"] == c.consumer.digest
        and value["snapshot"]["identity"]["uid"] == c.consumer.uid
    )


def baseline_history_check(response, context):
    assert response.status_code == 200, response.text
    assert any(row["id"] == context.consumer.baseline_id for row in response.json()["items"])


def context_check(response):
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "missing"
    assert response.json()["execution_eligible"] is False


def baseline_check(response, c, actor):
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["confirmed_by"] == c.actors[actor].session["principal"]["id"]
    with sessions()() as db:
        row = db.get(Baseline, UUID(value["id"]))
        assert row.workload_id == UUID(c.consumer.workload_id) and row.candidate_id == UUID(
            c.consumer.candidate_id
        )
        assert row.digest == c.consumer.digest


def note_check(response, c, actor):
    assert response.status_code == 201, response.text
    with sessions()() as db:
        row = db.get(IncidentActivity, UUID(response.json()["id"]))
        assert str(row.incident_id) == c.consumer.incident_id and row.text == "New consumer note"
        assert str(row.actor_id) == c.actors[actor].session["principal"]["id"]


def transition_check(response, c, actor):
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "acknowledged"
    with sessions()() as db:
        assert db.get(Incident, UUID(c.consumer.incident_id)).state == "acknowledged"
        assert (
            db.scalar(select(IncidentActivity).where(IncidentActivity.kind == "acknowledge")).text
            == "Investigating the saved incident"
        )


def read_plan_check(response, c, actor):
    assert response.status_code == 200, response.text
    assert response.json()["plan"]["id"] == c.consumer.plan_id
    with sessions()() as db:
        row = db.scalar(
            select(PlanReview).where(
                PlanReview.token_hash == token_hash(response.json()["review_token"])
            )
        )
        assert (
            row.session_binding == token_hash(c.actors[actor].session["csrf_token"])
            and not row.consumed
        )


def propose_check(response, c, actor):
    assert response.status_code == 201, response.text
    result = response.json()
    assert (
        result["content"]["before"]["uid"] == c.consumer.uid and result["content"]["replicas"] == 2
    )
    assert result["created_by"] == c.actors[actor].session["principal"]["id"]
    with sessions()() as db:
        assert db.get(ActionPlan, UUID(c.consumer.plan_id)).state == "invalidated"
        assert db.get(ActionPlan, UUID(result["id"])).state == "proposed"


def decide_check(response, c, actor):
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "approved"
    with sessions()() as db:
        execution = db.scalar(
            select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id))
        )
        assert execution.holds_lock and execution.state == "queued"
        assert (
            db.scalar(select(Job).where(Job.subject_id == execution.id)).kind
            == "recovery_execution_v1"
        )
        assert db.get(ActionPlan, execution.plan_id).decided_by == UUID(
            c.actors[actor].session["principal"]["id"]
        )


def report_check(response, c, actor):
    assert response.status_code == 200, response.text
    assert response.json()["id"] == c.consumer.report_id
    assert (
        response.json()["state"] == "insufficient_data"
        and response.json()["result"]["recommendations"] is None
    )


def report_create_check(response, c, actor):
    assert response.status_code == 201, response.text
    with sessions()() as db:
        row = db.get(RecommendationReport, UUID(response.json()["id"]))
        assert str(row.workload_id) == c.consumer.workload_id and row.state == "queued"
        assert str(row.created_by) == c.actors[actor].session["principal"]["id"]
        assert (
            db.scalar(select(Job).where(Job.subject_id == row.id)).kind
            == "recommendation_report_v1"
        )


def check_create_check(response, c, actor):
    assert response.status_code == 201, response.text
    with sessions()() as db:
        row = db.get(ConnectionCheck, UUID(response.json()["id"]))
        assert row.component == "database" and row.status == "available" and row.source == "runtime"
        assert str(row.created_by) == c.actors[actor].session["principal"]["id"]


def handoff_check(response, c, actor):
    assert response.status_code == 200 and "text/markdown" in response.headers["content-type"]
    assert c.consumer.incident_id in response.text and "Saved consumer note" in response.text
    assert "unknown outcomes" in response.text.lower()


def activity_check(response, c, actor):
    assert response.status_code == 200, response.text
    assert any(item["text"] == "Saved consumer note" for item in response.json()["items"])


def preview_check(response, c, actor):
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["incident_id"] == c.consumer.incident_id
    assert "Saved consumer note" in data["markdown"]


def cancel_check(response, c, actor):
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["id"] == c.consumer.pending_run_id
    assert value["state"] == "cancelled" and value["outcome_code"] == "operator_cancelled"
    assert value["queue_reason"] is None and value["attempts"] == []
    with sessions()() as db:
        row = db.scalar(
            select(IncidentActivity).where(
                IncidentActivity.incident_id == UUID(c.consumer.pending_incident_id),
                IncidentActivity.kind == "run_cancelled",
            )
        )
        assert row.text == "No longer needed"
        assert str(row.actor_id) == c.actors[actor].session["principal"]["id"]


CASES = {
    "cancel_run": RequestCase(
        lambda c: {
            "path_params": {
                "incident_id": c.consumer.pending_incident_id,
                "run_id": c.consumer.pending_run_id,
            },
            "json": {"reason": "No longer needed"},
        },
        cancel_check,
    ),
    "get_baseline_candidate": RequestCase(workload_path, candidate_check),
    "confirm_baseline": RequestCase(
        lambda c: {
            **workload_path(c),
            "json": {"candidate_id": c.consumer.candidate_id, "digest": c.consumer.digest},
        },
        baseline_check,
    ),
    "list_baselines": RequestCase(workload_path, lambda r, c, a: baseline_history_check(r, c)),
    "get_recovery_context": RequestCase(workload_path, lambda r, c, a: context_check(r)),
    "add_incident_note": RequestCase(
        lambda c: {**incident_path(c), "json": {"text": "New consumer note"}}, note_check
    ),
    "transition_incident": RequestCase(
        lambda c: {
            **incident_path(c),
            "json": {"action": "acknowledge", "reason": "Investigating the saved incident"},
        },
        transition_check,
    ),
    "list_incident_activity": RequestCase(incident_path, activity_check),
    "preview_incident_handoff": RequestCase(incident_path, preview_check),
    "export_incident_handoff": RequestCase(incident_path, handoff_check),
    "create_connection_check": RequestCase(
        lambda c: {"json": {"component": "database"}}, check_create_check
    ),
    "list_connection_checks": RequestCase(
        lambda c: {},
        lambda r, c, a: saved_list(r, c, a, "reason", "Persisted prior connection check."),
    ),
    "create_recommendation_report": RequestCase(
        lambda c: {"json": {"workload_id": c.consumer.workload_id, "requested_hours": 6}},
        report_create_check,
    ),
    "list_recommendation_reports": RequestCase(
        lambda c: {}, lambda r, c, a: saved_list(r, c, a, "id", c.consumer.report_id)
    ),
    "get_recommendation_report": RequestCase(report_path, report_check),
    "export_recommendation_report": RequestCase(report_path, report_check),
    "get_recovery_policy": RequestCase(
        lambda c: {},
        lambda r, c, a: PolicyView.model_validate(r.json()),
    ),
    "read_recovery_availability": RequestCase(
        workload_path, lambda r, c, a: recovery_availability_check(r, c)
    ),
    "read_recovery_execution": RequestCase(
        lambda c: {"path_params": {"execution_id": c.consumer.execution_id}},
        lambda r, c, a: ExecutionView.model_validate(r.json()),
    ),
    "propose_plan": RequestCase(
        lambda c: {
            **incident_path(c),
            "json": {"replicas": 2, "rationale": "Explicit permission fixture proposal"},
        },
        propose_check,
    ),
    "list_plans": RequestCase(
        incident_path, lambda r, c, a: saved_list(r, c, a, "id", c.consumer.plan_id)
    ),
    "read_plan": RequestCase(plan_path, read_plan_check),
    "decide_plan": RequestCase(
        lambda c: {
            **plan_path(c),
            "json": {
                "version": 1,
                "digest": c.consumer.plan_digest,
                "review_token": c.consumer.review_token,
                "decision": "approve",
            },
        },
        decide_check,
    ),
    "list_executions": RequestCase(
        incident_path, lambda r, c, a: saved_list(r, c, a, "id", c.consumer.execution_id)
    ),
    "read_recovery_verification": RequestCase(
        lambda c: {"path_params": {"execution_id": c.consumer.execution_id}},
        lambda r, c, a: missing_legacy_verification(r),
    ),
    "read_rollback_eligibility": RequestCase(
        lambda c: {"path_params": {"execution_id": c.consumer.execution_id}},
        lambda r, c, a: legacy_rollback_check(r),
    ),
    "propose_inverse_plan": RequestCase(
        lambda c: {
            "path_params": {"execution_id": c.consumer.execution_id},
            "json": {"rationale": "Permission boundary for legacy unavailable rollback"},
        },
        lambda r, c, a: unavailable_inverse_check(r),
    ),
}


def legacy_rollback_check(response):
    assert response.status_code == 200, response.text
    assert not response.json()["can_propose"]
    assert response.json()["eligible_from"] is None and response.json()["deadline"] is None


def recovery_availability_check(response, context):
    assert response.status_code == 200, response.text
    value = RecoveryAvailability.model_validate(response.json())
    assert str(value.workload_id) == context.consumer.workload_id
    assert value.uid == context.consumer.uid


def unavailable_inverse_check(response):
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "inverse_unavailable"


def missing_legacy_verification(response):
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "verification_not_found"
