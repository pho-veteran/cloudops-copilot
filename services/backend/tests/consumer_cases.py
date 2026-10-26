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
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recovery import client as gateway_client
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.workloads.kubernetes import Kubernetes, snapshot
from cloudops.features.workloads.models import Baseline, BaselineCandidate, Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job

CONSUMER_MODELS = (
    Workload,
    BaselineCandidate,
    Baseline,
    IncidentActivity,
    RecommendationReport,
    ConnectionCheck,
    ActionPlan,
    Execution,
    PlanReview,
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
            digest=digest,
            baseline_id=str(baseline.id),
            report_id=str(report.id),
            plan_id=str(plan.id),
            plan_digest=plan_digest,
            review_token=review_token,
            execution_id=str(execution.id),
            deployment=deployment,
        )
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
    assert value["digest"] == c.consumer.digest and value["snapshot"]["uid"] == c.consumer.uid


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
    assert "historical" in response.text.lower() and "new diagnosis" in response.text


CASES = {
    "get_baseline_candidate": RequestCase(workload_path, candidate_check),
    "confirm_baseline": RequestCase(
        lambda c: {
            **workload_path(c),
            "json": {"candidate_id": c.consumer.candidate_id, "digest": c.consumer.digest},
        },
        baseline_check,
    ),
    "list_baselines": RequestCase(
        workload_path, lambda r, c, a: saved_list(r, c, a, "id", c.consumer.baseline_id)
    ),
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
    "list_incident_activity": RequestCase(
        incident_path, lambda r, c, a: saved_list(r, c, a, "text", "Saved consumer note")
    ),
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
}
