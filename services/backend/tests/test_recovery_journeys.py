"""Persisted API projections and history; gateway observations here are controlled."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_permissions import permission_context as _permission_context

from cloudops.features.access.service import token_hash
from cloudops.features.recovery import client
from cloudops.features.recovery.models import (
    ActionPlan,
    Execution,
    PlanReview,
    Verification,
    RollbackWindow,
)
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError

permission_context = _permission_context


@pytest.mark.parametrize("enabled", [True, False])
def test_current_authority_is_separate_from_an_old_unknown_lock(
    permission_context, monkeypatch, enabled
):
    c = permission_context
    identifier = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        old = db.get(Execution, identifier)
        old.state, old.holds_lock = "outcome_unknown", True
        old.result = {
            "outcome": "outcome_unknown",
            "verification": "inconclusive",
            "reason": "Controlled missing receipt; not a live write.",
        }
        old.created_at = database_now(db) - timedelta(days=1)
        original = db.get(ActionPlan, old.plan_id)
        for _ in range(26):
            plan = ActionPlan(
                incident_id=original.incident_id,
                workload_id=original.workload_id,
                content={"source": "controlled history"},
                digest="a" * 64,
                target_epoch="fixture",
                rationale="Controlled recent terminal history",
                created_by=original.created_by,
                expires_at=database_now(db),
            )
            db.add(plan)
            db.flush()
            db.add(
                Execution(
                    plan_id=plan.id,
                    workload_id=plan.workload_id,
                    state="manual_intervention",
                    holds_lock=False,
                    result={"outcome": "denied"},
                )
            )
    monkeypatch.setattr(
        client,
        "target",
        lambda uid: {
            "uid": uid,
            "epoch": "fixture",
            "enabled": enabled,
            "recovery_mode": not enabled,
        },
    )
    viewer = c.actors["viewer"].client
    data = viewer.get(f"/api/workloads/{c.consumer.workload_id}/recovery-availability")
    assert data.status_code == 200, data.text
    value = data.json()
    assert value["target_authority"] == ("enabled" if enabled else "quarantined")
    assert value["application_state"] == "blocked"
    assert value["blocking_executions"][0]["id"] == str(identifier)
    assert value["blocking_executions"][0]["outcome"]["attribution"] == "outcome_unknown"
    recent = viewer.get(f"/api/incidents/{c.consumer.incident_id}/executions").json()
    assert len(recent) == 25 and str(identifier) not in {row["id"] for row in recent}
    older = viewer.get(f"/api/incidents/{c.consumer.incident_id}/executions?offset=25").json()
    assert str(identifier) in {row["id"] for row in older}
    exact = viewer.get(f"/api/executions/{identifier}")
    assert exact.status_code == 200 and exact.json()["id"] == str(identifier)
    assert exact.json()["outcome"]["future_authority"] == "target_locked"
    assert viewer.get(f"/api/incidents/{c.consumer.incident_id}/plans?offset=25").status_code == 200
    assert len(value["recent_executions"]) == 5
    assert str(identifier) not in {row["id"] for row in value["recent_executions"]}
    page = viewer.get("/api/incidents?view=all").json()
    queued = next(row for row in page["items"] if row["id"] == c.consumer.incident_id)
    assert len(queued["recent_executions"]) == 5
    assert str(identifier) in {row["id"] for row in queued["relevant_executions"]}


@pytest.mark.parametrize("kind", ["missing", "malformed", "replacement"])
def test_unavailable_target_is_never_rendered_as_enabled(permission_context, monkeypatch, kind):
    c = permission_context

    def target(uid):
        if kind == "missing":
            raise DomainError(503, "executor_unavailable", "Controlled unavailable gateway.")
        return (
            {
                "uid": str(uuid4()) if kind == "replacement" else uid,
                "epoch": "fixture",
                "enabled": True,
                "recovery_mode": False,
            }
            if kind != "malformed"
            else {"enabled": True}
        )

    monkeypatch.setattr(client, "target", target)
    response = c.actors["viewer"].client.get(
        f"/api/workloads/{c.consumer.workload_id}/recovery-availability"
    )
    assert response.status_code == 200, response.text
    assert response.json()["target_authority"] == "unavailable"
    assert response.json()["application_state"] == "unavailable"
    assert response.json()["epoch"] is None


def test_synthetic_scope_and_invalid_history_inputs_are_honest(permission_context, monkeypatch):
    c = permission_context

    def forbidden(*_args):
        raise AssertionError("Synthetic availability must not query a live gateway")

    monkeypatch.setattr(client, "target", forbidden)
    viewer = c.actors["viewer"].client
    value = viewer.get(f"/api/workloads/{c.workload_id}/recovery-availability")
    assert value.status_code == 200 and value.json()["target_authority"] == "unavailable"
    for path in ["plans", "executions"]:
        assert (
            viewer.get(f"/api/incidents/{c.consumer.incident_id}/{path}?offset=-1").status_code
            == 422
        )
    assert viewer.get(f"/api/executions/{uuid4()}").status_code == 404


@pytest.mark.parametrize(
    "state, attribution, verification",
    [
        ("failed", "acknowledged", "failed"),
        ("no_op", "not_sent", "not_performed"),
        ("manual_intervention", "acknowledged", "inconclusive"),
        ("rolled_back", "acknowledged", "passed"),
    ],
)
def test_released_terminal_outcomes_remain_visible_without_blocking(
    permission_context, monkeypatch, state, attribution, verification
):
    c = permission_context
    identifier = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        row = db.get(Execution, identifier)
        row.state, row.holds_lock = state, False
        row.created_at = database_now(db) - timedelta(minutes=2)
        row.result = {
            "outcome": attribution,
            "verification": verification,
            "reason": "Controlled terminal outcome projection.",
        }
    monkeypatch.setattr(
        client,
        "target",
        lambda uid: {
            "uid": uid,
            "epoch": "fixture",
            "enabled": True,
            "recovery_mode": False,
        },
    )
    viewer = c.actors["viewer"].client
    value = viewer.get(f"/api/workloads/{c.consumer.workload_id}/recovery-availability").json()
    assert value["application_state"] == "ready_for_preflight"
    assert value["blocking_executions"] == []
    recent = value["recent_executions"][0]
    assert recent["id"] == str(identifier) and recent["state"] == state
    assert recent["outcome"]["attribution"] == attribution
    assert recent["outcome"]["verification"] == verification
    page = viewer.get("/api/incidents?view=all").json()
    incident = next(row for row in page["items"] if row["id"] == c.consumer.incident_id)
    assert incident["recent_executions"][0]["id"] == str(identifier)
    assert incident["recent_executions"][0]["outcome"]["verification"] == verification
    if state != "manual_intervention":
        assert str(identifier) not in {row["id"] for row in incident["relevant_executions"]}


def test_review_clock_reports_the_stored_receipt_deadline(permission_context):
    c = permission_context
    response = c.actors["operator"].client.get(f"/api/plans/{c.consumer.plan_id}")
    assert response.status_code == 200, response.text
    value = response.json()
    now = datetime.fromisoformat(value["server_time"])
    expires = datetime.fromisoformat(value["review_expires_at"])
    with sessions()() as db:
        receipt = db.scalar(
            select(PlanReview).where(PlanReview.token_hash == token_hash(value["review_token"]))
        )
        assert receipt.expires_at == expires
        assert expires <= db.get(ActionPlan, receipt.plan_id).expires_at
    assert timedelta(0) < expires - now <= timedelta(seconds=60)


def test_handoff_projects_fixed_verification_and_inverse_records(permission_context):
    c = permission_context
    identifier = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        now = database_now(db)
        db.add(
            Verification(
                execution_id=identifier,
                profile={"id": "controlled-profile", "version": "1"},
                profile_digest="b" * 64,
                acknowledged_at=now - timedelta(seconds=181),
                deadline=now - timedelta(seconds=1),
                next_slot=7,
                samples={},
                result={
                    "state": "inconclusive",
                    "valid_rollout_samples": 0,
                    "expected_rollout_samples": 5,
                },
                finalized_at=now,
            )
        )
        db.add(
            RollbackWindow(
                forward_execution_id=identifier,
                eligible_from=now,
                deadline=now + timedelta(seconds=599),
                policy_digest="c" * 64,
            )
        )
    response = c.actors["viewer"].client.get(
        f"/api/incidents/{c.consumer.incident_id}/handoff/preview"
    )
    assert response.status_code == 200, response.text
    value = response.json()
    row = next(
        row for row in value["snapshot"]["actions"]["executions"] if row["id"] == str(identifier)
    )
    assert row["verification_evidence"]["state"] == "inconclusive"
    assert row["verification_evidence"]["next_slot"] == 7
    assert row["recorded_inverse_window"]["inverse_execution_id"] is None
    assert (
        row["recorded_inverse_window"]["deadline"] > row["recorded_inverse_window"]["eligible_from"]
    )
    assert "This snapshot grants no authority" in value["markdown"]
