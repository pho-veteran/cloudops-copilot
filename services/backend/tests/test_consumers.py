"""Consumer invariants with real auth/transactions and controlled Kubernetes observations."""

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_permissions import ORIGIN, permission_context as _permission_context, send, snapshot

from cloudops.features.access.models import LoginSession
from cloudops.entrypoints.dispatch import GATE
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recovery import client as gateway_client
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.recovery.service import ExecutionHandler
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.models import BaselineCandidate
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, claim_next

permission_context = _permission_context


def decision(context, **changes):
    return {
        "version": 1,
        "digest": context.consumer.plan_digest,
        "review_token": context.consumer.review_token,
        "decision": "approve",
        **changes,
    }


def decide(context, body=None):
    return send(
        context,
        "decide_plan",
        "operator",
        {"path_params": {"plan_id": context.consumer.plan_id}, "json": body or decision(context)},
    )


@pytest.mark.parametrize("kind", ["expiry", "revocation"])
def test_approval_requires_fresh_plan_receipt_after_new_session(permission_context, kind):
    c = permission_context
    old = decision(c)
    with sessions()() as db, db.begin():
        row = db.scalar(
            select(LoginSession).where(
                LoginSession.csrf_token == c.actors["operator"].session["csrf_token"]
            )
        )
        if kind == "expiry":
            row.expires_at = database_now(db) - timedelta(seconds=1)
        else:
            db.delete(row)
    before = snapshot()
    assert decide(c, old).status_code == 401
    client = c.actors["operator"].client
    session = client.post(
        "/api/login",
        json={"username": "operator", "password": "local-test-password"},
        headers={"Origin": ORIGIN},
    ).json()
    headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    path = f"/api/plans/{c.consumer.plan_id}/decision"
    response = client.post(path, json=old, headers=headers)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "plan_refetch_required"
    )
    assert snapshot() == before
    fresh = client.get(f"/api/plans/{c.consumer.plan_id}").json()
    assert fresh["valid"] and fresh["review_token"] != old["review_token"]
    assert (
        client.post(
            path, json={**old, "review_token": fresh["review_token"]}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            path, json={**old, "review_token": fresh["review_token"]}, headers=headers
        ).status_code
        == 409
    )
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.plan_id == UUID(c.consumer.plan_id))
            )
            == 1
        )
        assert db.get(ActionPlan, UUID(c.consumer.plan_id)).state == "approved"


@pytest.mark.parametrize(
    "reason",
    [
        "version",
        "digest",
        "expired_plan",
        "expired_receipt",
        "live_changed",
        "controller",
        "epoch",
        "quarantine",
        "queue_full",
        "cooldown",
    ],
)
def test_invalid_approval_never_consumes_receipt_or_admits_execution(
    permission_context, monkeypatch, reason
):
    c = permission_context
    body = decision(c)
    if reason in {"expired_plan", "expired_receipt", "cooldown"}:
        with sessions()() as db, db.begin():
            if reason == "expired_plan":
                db.get(ActionPlan, UUID(c.consumer.plan_id)).expires_at = database_now(
                    db
                ) - timedelta(seconds=1)
            elif reason == "expired_receipt":
                db.execute(
                    update(PlanReview).values(expires_at=database_now(db) - timedelta(seconds=1))
                )
            else:
                db.get(Execution, UUID(c.consumer.execution_id)).created_at = database_now(db)
    if reason == "version":
        body["version"] = 2
    if reason == "digest":
        body["digest"] = "f" * 64
    if reason == "live_changed":
        c.consumer.deployment["metadata"]["resourceVersion"] = "new-fixture-rv"
        c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "example/fixture:changed"
        )
    if reason == "controller":

        def controller(*_args):
            raise DomainError(409, "controller_conflict", "Controlled controller conflict.")

        monkeypatch.setattr(Kubernetes, "controllers", controller)
    if reason in {"epoch", "quarantine"}:
        monkeypatch.setattr(
            gateway_client,
            "target",
            lambda uid: {
                "uid": uid,
                "enabled": reason != "quarantine",
                "recovery_mode": False,
                "epoch": "retired-epoch",
            },
        )
    if reason == "queue_full":
        with sessions()() as db, db.begin():
            db.add_all(
                Job(kind="queued_fixture", subject_id=uuid4())
                for _ in range(settings().queue_limit)
            )
    before = snapshot()
    response = decide(c, body)
    assert response.status_code == 409, response.text
    assert snapshot() == before


@pytest.mark.parametrize("change", ["too_young", "expired", "digest", "snapshot", "unhealthy"])
def test_baseline_requires_two_matching_healthy_observations(permission_context, change):
    c = permission_context
    if change in {"too_young", "expired"}:
        with sessions()() as db, db.begin():
            db.get(BaselineCandidate, UUID(c.consumer.candidate_id)).created_at = database_now(
                db
            ) - timedelta(seconds=0 if change == "too_young" else 301)
            if change == "expired":
                row = db.get(BaselineCandidate, UUID(c.consumer.candidate_id))
                value = copy.deepcopy(row.snapshot)
                value["provenance"]["first"]["observed_at"] = (
                    database_now(db) - timedelta(seconds=301)
                ).isoformat()
                row.snapshot = value
    body = {"candidate_id": c.consumer.candidate_id, "digest": c.consumer.digest}
    if change == "digest":
        body["digest"] = "0" * 64
    if change == "snapshot":
        c.consumer.deployment["spec"]["replicas"] = 2
    if change == "unhealthy":
        c.consumer.deployment["status"]["readyReplicas"] = 0
    before = snapshot()
    response = send(
        c,
        "confirm_baseline",
        "operator",
        {"path_params": {"workload_id": c.consumer.workload_id}, "json": body},
    )
    assert response.status_code == 409, response.text
    assert snapshot() == before


def test_resolution_is_immutable_and_does_not_release_unknown_target(permission_context):
    c = permission_context
    with sessions()() as db, db.begin():
        row = db.get(Execution, UUID(c.consumer.execution_id))
        row.state, row.holds_lock, row.result = (
            "outcome_unknown",
            True,
            {"outcome": "outcome_unknown", "reason": "Controlled lost acknowledgement."},
        )
        db.add(Job(kind="recovery_execution_v1", subject_id=row.id, state="completed"))

    def transition(action):
        return send(
            c,
            "transition_incident",
            "operator",
            {
                "path_params": {"incident_id": c.consumer.incident_id},
                "json": {"action": action, "reason": "Explicit lifecycle verification"},
            },
        )

    assert transition("resolve").status_code == 200
    activity = send(c, "list_incident_activity", "viewer").json()["items"]
    resolution = next(a for a in activity if a["kind"] == "resolve")
    path = f"/api/incidents/{c.consumer.incident_id}/handoff?resolution_id={resolution['id']}"
    saved = c.actors["viewer"].client.get(path).content
    assert b"outcome_unknown" in saved and b"Controlled lost acknowledgement" in saved
    assert transition("reopen").status_code == 200
    assert send(c, "add_incident_note", "operator").status_code == 201
    assert c.actors["viewer"].client.get(path).content == saved
    with sessions()() as db:
        assert db.get(Execution, UUID(c.consumer.execution_id)).holds_lock
    assert send(c, "propose_plan", "operator").json()["error"]["code"] == "target_locked"


def test_active_execution_blocks_resolution_and_reinvestigation(permission_context):
    c = permission_context
    assert decide(c).status_code == 200
    assert claim_next({"recovery_execution_v1": ExecutionHandler()}, GATE) is not None
    before = snapshot()
    response = send(
        c,
        "transition_incident",
        "operator",
        {
            "path_params": {"incident_id": c.consumer.incident_id},
            "json": {"action": "resolve", "reason": "Cannot hide active execution"},
        },
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "execution_active"
    response = c.actors["operator"].client.post(
        f"/api/incidents/{c.consumer.incident_id}/runs",
        json={"workload_id": c.consumer.workload_id, "reason": "Re-check during execution"},
        headers={**c.actors["operator"].headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "execution_active"
    assert snapshot() == before


def test_concurrent_decisions_create_one_execution_and_job(permission_context):
    c = permission_context

    def approve(_):
        with TestClient(c.app) as client:
            return client.post(
                f"/api/plans/{c.consumer.plan_id}/decision",
                json=decision(c),
                headers={
                    **c.actors["operator"].headers,
                    "Cookie": f"cloudops_session={c.actors['operator'].cookie}",
                },
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, range(2)))
    assert sorted(results) == [200, 409]
    with sessions()() as db:
        execution = db.scalars(
            select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id))
        ).one()
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.subject_id == execution.id))
            == 1
        )


def test_late_worker_cannot_reclassify_or_relock_maintenance_released_unknown(permission_context):
    c = permission_context
    assert decide(c).status_code == 200
    with sessions()() as db, db.begin():
        row = db.scalar(select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id)))
        identity = row.id
        row.state, row.holds_lock, row.maintenance_released = "outcome_unknown", False, True
        row.result = {"outcome": "outcome_unknown", "maintenance_request_id": str(uuid4())}
    before = snapshot()
    with sessions()() as db, db.begin():
        handler = ExecutionHandler()
        handler.started(db, identity)
        handler.publish(db, identity, {"outcome": "acknowledged"})
        handler.failed(db, identity, "late worker", terminal=True)
    assert snapshot() == before


def test_duplicate_report_admission_rolls_back_without_second_job(permission_context):
    c = permission_context
    assert send(c, "create_recommendation_report", "operator").status_code == 201
    before = snapshot()
    response = send(c, "create_recommendation_report", "operator")
    assert response.status_code == 409 and response.json()["error"]["code"] == "report_active"
    assert snapshot() == before
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(RecommendationReport)
                .where(RecommendationReport.state == "queued")
            )
            == 1
        )


@pytest.mark.parametrize("role", ["anonymous", "viewer", "operator"])
def test_no_application_maintenance_route_exists(permission_context, role):
    c = permission_context
    before = snapshot()
    response = c.actors[role].client.post(
        "/api/maintenance/enable_target",
        json={"actor": "root", "target": c.consumer.uid},
        headers=c.actors[role].headers,
    )
    assert response.status_code == 404
    assert snapshot() == before
