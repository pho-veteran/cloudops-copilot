"""Real PostgreSQL/session/job checks; external observations and ACKs are controlled."""

import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from test_recovery_verification import (
    qualified as _qualified,
    candidate as _candidate,
    permission_context as _permission_context,
    approve,
    start,
    controlled_capture,
)

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.recovery import rollback, service, verification, verification_policy
from cloudops.features.recovery.models import ActionPlan, Execution, Verification, RollbackWindow
from cloudops.features.recovery.verification_policy import VERIFICATION_JOB
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, claim_next, finish
from cloudops.features.investigations.collector import Interrupted

qualified, candidate, permission_context = _qualified, _candidate, _permission_context


@pytest.fixture
def reversible(qualified, monkeypatch):
    c = qualified
    p = c.verification_profile.model_copy(update={"replicas": [1, 2]})
    c.recovery_rules.update(inverse_policy="captured-old-state/v1", inverse_window_seconds=600)
    c.recovery_path.write_text(json.dumps(c.recovery_rules))

    def lookup(uid, template, desired):
        return p if p.matches(uid, template, desired) else None

    monkeypatch.setattr(verification_policy, "load_for", lookup)
    monkeypatch.setattr("cloudops.features.recovery.plans.load_for", lookup)
    monkeypatch.setattr(rollback, "load_for", lookup)
    return c


def final_forward(c, monkeypatch, *, state="passed", early=False, unknown=False):
    identifier, calls = start(c, monkeypatch)
    with sessions()() as db, db.begin():
        row = db.get(Execution, identifier)
        record = db.get(Verification, identifier)
        now = database_now(db)
        anchor = now - timedelta(seconds=181)
        record.acknowledged_at, record.deadline = anchor, anchor + timedelta(seconds=180)
        row.dispatch_started_at = anchor - timedelta(seconds=1)
        row.created_at = now - timedelta(seconds=40)
        receipt = {**row.result["gateway_receipt"], "acknowledged_at": anchor.isoformat()}
        row.result = {**row.result, "gateway_receipt": receipt}
        if early:
            record.deadline = now + timedelta(seconds=90)
        if unknown:
            row.result = {
                **row.result,
                "attribution": "outcome_unknown",
                "outcome": "outcome_unknown",
            }
        verification.finalize(
            db,
            row,
            record,
            {"state": state, "fixture": "Controlled finalized observation; no live claim"},
        )
    c.consumer.deployment["spec"]["replicas"] = 2
    return identifier, calls


def inverse(c, identifier, **payload):
    actor = c.actors["operator"]
    return actor.client.post(
        f"/api/executions/{identifier}/inverse-plans",
        headers=actor.headers,
        json={"rationale": "Fresh explicitly approved captured inverse", **payload},
    )


def test_separate_inverse_approval_and_fresh_verification_leave_forward_unchanged(
    reversible, monkeypatch
):
    c = reversible
    actor = c.actors["operator"]
    saved_path = f"/api/incidents/{c.incident_id}"
    assert (
        actor.client.post(
            saved_path + "/lifecycle",
            headers=actor.headers,
            json={
                "action": "resolve",
                "reason": "Preserve saved bytes while another incident rolls back.",
            },
        ).status_code
        == 200
    )
    handoff = actor.client.get(saved_path + "/handoff").content
    report_path = f"/api/recommendation-reports/{c.consumer.report_id}/export"
    report = actor.client.get(report_path).content
    original, calls = final_forward(c, monkeypatch)
    actor = c.actors["operator"]
    with sessions()() as db:
        forward_before = copy.deepcopy(db.get(Execution, original).result)
        window = db.get(RollbackWindow, original)
        assert window and window.deadline - window.eligible_from < timedelta(seconds=600)
        clock_before = window.eligible_from, window.deadline
    eligibility = actor.client.get(f"/api/executions/{original}/rollback").json()
    assert eligibility["can_propose"]
    response = inverse(c, original)
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["origin"] == "inverse_rollback" and plan["forward_execution_id"] == str(original)
    assert plan["content"]["before"]["replicas"] == 2
    assert plan["content"]["desired"]["replicas"] == 1
    assert plan["content"]["selector"]["forward_operation_id"] == str(original)
    identifier = approve(c, plan)

    def healthy(_id, _incident, workload, start, end, **kwargs):
        value = controlled_capture(
            c,
            workload,
            datetime.now(timezone.utc),
            replicas=2,
            start=start,
            end=end,
            include_metrics=True,
        )
        return value

    monkeypatch.setattr(verification.sources, "capture", healthy)
    handler = service.ExecutionHandler()
    claim = claim_next({"recovery_execution_v1": handler}, GATE)
    assert claim and claim.subject_id == identifier
    with sessions()() as db:
        assert db.get(Execution, identifier).state == "rolling_back"
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=actor.headers,
        json={"action": "resolve", "reason": "Must not hide an active inverse."},
    )
    assert response.status_code == 409
    result = service.execute(identifier, before_call=lambda: None)
    assert result["outcome"] == "acknowledged" and len(calls) == 2
    assert calls[-1]["direction"] == "inverse"
    assert finish(claim, handler, result=result, gate=GATE)
    with sessions()() as db:
        record = db.get(Verification, identifier)
        anchor = record.acknowledged_at
    c.consumer.deployment["spec"]["replicas"] = 1
    clock = [anchor]
    monkeypatch.setattr(verification, "database_now", lambda db: clock[0])

    def recovered(_id, _incident, workload, start, end, **kwargs):
        value = controlled_capture(
            c,
            workload,
            clock[0],
            replicas=1,
            start=start + timedelta(seconds=60),
            end=end,
            include_metrics=kwargs.get("profile") is not None,
        )
        if "metrics" in value:
            value["metrics"]["error_ratio"]["series"][0]["samples"] = [
                [stamp, 0] for stamp, _ in value["metrics"]["error_ratio"]["series"][0]["samples"]
            ]
        return value

    monkeypatch.setattr(verification.sources, "capture", recovered)
    for slot in range(7):
        clock[0] = anchor + timedelta(seconds=slot * 30)
        with sessions()() as db, db.begin():
            db.scalar(
                select(Job).where(Job.kind == VERIFICATION_JOB, Job.subject_id == identifier)
            ).due_at = database_now(db) - timedelta(seconds=1)
        verification_claim = claim_next(
            {VERIFICATION_JOB: verification.VerificationHandler()}, GATE
        )
        assert verification_claim and verification_claim.subject_id == identifier
        verification.verify(verification_claim, GATE, threading.Event())
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "rolled_back" and not row.holds_lock, row.result
        assert db.get(Execution, original).result == forward_before
        window = db.get(RollbackWindow, original)
        assert (window.eligible_from, window.deadline) == clock_before
        assert window.inverse_execution_id == identifier
        assert db.get(RollbackWindow, identifier) is None
    assert inverse(c, original).status_code == 409
    assert actor.client.get(saved_path + "/handoff").content == handoff
    assert actor.client.get(report_path).content == report


@pytest.mark.parametrize("state", ["passed", "failed", "inconclusive"])
def test_first_finalization_window_never_rearms(reversible, monkeypatch, state):
    c = reversible
    identifier, _ = final_forward(c, monkeypatch, state=state)
    with sessions()() as db, db.begin():
        window = db.get(RollbackWindow, identifier)
        expected = window.eligible_from, window.deadline
        verification.finalize(
            db, db.get(Execution, identifier), db.get(Verification, identifier), {"state": "passed"}
        )
    for _ in range(2):
        assert (
            c.actors["viewer"].client.get(f"/api/executions/{identifier}/rollback").status_code
            == 200
        )
    with sessions()() as db:
        window = db.get(RollbackWindow, identifier)
        assert (window.eligible_from, window.deadline) == expected


@pytest.mark.parametrize("kind", ["early", "unknown", "legacy", "unsafe", "missing_profile"])
def test_no_authority_from_early_unknown_legacy_or_unsafe_result(reversible, monkeypatch, kind):
    c = reversible
    if kind == "legacy":
        c.recovery_rules.pop("inverse_window_seconds")
        c.recovery_path.write_text(json.dumps(c.recovery_rules))
    if kind == "unsafe":
        c.recovery_rules["max_memory"] = "32Mi"
        c.recovery_rules["memory_profiles"] = []
        c.recovery_path.write_text(json.dumps(c.recovery_rules))
    if kind == "missing_profile":
        original_lookup = rollback.load_for
        monkeypatch.setattr(
            rollback,
            "load_for",
            lambda uid, template, desired: None
            if desired["replicas"] == 1
            else original_lookup(uid, template, desired),
        )
    identifier, _ = final_forward(c, monkeypatch, early=kind == "early", unknown=kind == "unknown")
    assert inverse(c, identifier).status_code == 409
    with sessions()() as db:
        assert db.get(RollbackWindow, identifier) is None


@pytest.mark.parametrize(
    "change",
    ["expired", "uid", "fields", "epoch", "controller", "capacity", "policy", "unsupported_config"],
)
def test_fresh_inverse_boundaries_deny_before_approval(reversible, monkeypatch, change):
    c = reversible
    identifier, _ = final_forward(c, monkeypatch)
    if change == "expired":
        with sessions()() as db, db.begin():
            window = db.get(RollbackWindow, identifier)
            window.eligible_from -= timedelta(seconds=601)
            window.deadline -= timedelta(seconds=601)
    elif change == "uid":
        c.consumer.deployment["metadata"]["uid"] = "replacement"
    elif change == "fields":
        c.consumer.deployment["spec"]["replicas"] = 3
    elif change == "unsupported_config":
        c.consumer.deployment["spec"]["template"]["spec"]["volumes"] = [
            {
                "name": "api-token",
                "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
            }
        ]
    elif change == "policy":
        c.recovery_rules["inverse_policy"] = "unavailable"
        c.recovery_path.write_text(json.dumps(c.recovery_rules))
    elif change == "epoch":
        monkeypatch.setattr(
            service.client,
            "target",
            lambda uid: {"enabled": True, "recovery_mode": False, "epoch": "retired"},
        )
    else:
        from cloudops.features.workloads.kubernetes import Kubernetes

        def denied(*args):
            raise DomainError(409, "controlled_denial", change)

        monkeypatch.setattr(
            Kubernetes, "controllers" if change == "controller" else "capacity_budget", denied
        )
    response = inverse(c, identifier)
    assert response.status_code == 409, response.text


def test_forward_receipt_and_caller_values_cannot_authorize_inverse(reversible, monkeypatch):
    c = reversible
    identifier, _ = final_forward(c, monkeypatch)
    assert inverse(c, identifier, replicas=3).status_code == 422
    plan = inverse(c, identifier).json()
    actor, viewer = c.actors["operator"], c.actors["viewer"]
    path = f"/api/plans/{plan['id']}"
    foreign_review = viewer.client.get(path).json()
    body = {
        "version": plan["version"],
        "digest": plan["digest"],
        "review_token": foreign_review["review_token"],
        "decision": "approve",
    }
    assert (
        actor.client.post(path + "/decision", headers=actor.headers, json=body).status_code == 409
    )
    assert (
        viewer.client.post(path + "/decision", headers=viewer.headers, json=body).status_code == 403
    )
    body["review_token"] = actor.client.get(path).json()["review_token"]
    with sessions()() as db:
        original_plan = db.get(ActionPlan, db.get(Execution, identifier).plan_id)
        body["digest"] = original_plan.digest
    assert (
        actor.client.post(path + "/decision", headers=actor.headers, json=body).status_code == 409
    )
    body["digest"] = plan["digest"]
    response = actor.client.post(path + "/decision", headers=actor.headers, json=body)
    assert response.status_code == 200, response.text
    assert (
        actor.client.post(path + "/decision", headers=actor.headers, json=body).status_code == 409
    )


def test_expiry_after_approval_before_claim_retires_without_send(reversible, monkeypatch):
    c = reversible
    original, calls = final_forward(c, monkeypatch)
    plan = inverse(c, original).json()
    identifier = approve(c, plan)
    with sessions()() as db, db.begin():
        window = db.get(RollbackWindow, original)
        window.eligible_from -= timedelta(seconds=601)
        window.deadline -= timedelta(seconds=601)
    assert claim_next({"recovery_execution_v1": service.ExecutionHandler()}, GATE) is None
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "manual_intervention" and row.dispatch_started_at is None
        assert row.result["attribution"] == "not_sent"
    assert len(calls) == 1


@pytest.mark.parametrize("cut", ["after_admission", "dropped_ack"])
def test_unknown_inverse_is_receipt_only_and_keeps_original_authority(reversible, monkeypatch, cut):
    c = reversible
    original, _ = final_forward(c, monkeypatch)
    identifier = approve(c, inverse(c, original).json())
    calls = []

    def gateway(path, request=None):
        calls.append((path, request))
        raise DomainError(
            503 if request else 404, "controlled_loss", "Controlled lost inverse response."
        )

    monkeypatch.setattr(service.client, "gateway", gateway)
    handler = service.ExecutionHandler()
    claim = claim_next({"recovery_execution_v1": handler}, GATE)
    assert claim and claim.subject_id == identifier

    def boundary():
        with sessions()() as db:
            if (
                cut == "after_admission"
                and db.get(Execution, identifier).dispatch_started_at is not None
            ):
                raise Interrupted()

    if cut == "after_admission":
        with pytest.raises(Interrupted):
            service.execute(identifier, before_call=boundary)
        assert calls == []
    else:
        assert service.execute(identifier, before_call=boundary)["outcome"] == "outcome_unknown"
        assert len(calls) == 1 and calls[0][0] == "/dispatch"
    with sessions()() as db:
        row = db.get(Execution, identifier)
        request, started, deadline = (
            copy.deepcopy(row.dispatch_request),
            row.dispatch_started_at,
            row.reconcile_deadline,
        )
    outcome = service.execute(identifier, before_call=lambda: None)
    assert outcome["outcome"] == "outcome_unknown"
    assert sum(path == "/dispatch" for path, _ in calls) == int(cut == "dropped_ack")
    assert finish(claim, handler, result=outcome, gate=GATE)
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "reconciling" and row.holds_lock
        assert (row.dispatch_request, row.dispatch_started_at, row.reconcile_deadline) == (
            request,
            started,
            deadline,
        )
        assert db.get(RollbackWindow, original).inverse_execution_id == identifier
    assert inverse(c, original).status_code == 409


def test_rejected_inverse_can_be_replaced_within_original_window(reversible, monkeypatch):
    c = reversible
    original, _ = final_forward(c, monkeypatch)
    first = inverse(c, original).json()
    actor = c.actors["operator"]
    path = f"/api/plans/{first['id']}"
    reviewed = actor.client.get(path).json()
    result = actor.client.post(
        path + "/decision",
        headers=actor.headers,
        json={
            "version": first["version"],
            "digest": first["digest"],
            "review_token": reviewed["review_token"],
            "decision": "reject",
        },
    )
    assert result.status_code == 200 and result.json()["state"] == "rejected"
    second = inverse(c, original)
    assert second.status_code == 201 and second.json()["id"] != first["id"]
    assert (
        second.json()["expires_at"]
        <= actor.client.get(f"/api/executions/{original}/rollback").json()["deadline"]
    )


def test_inverse_migration_preserves_legacy_records_and_refuses_authority_loss(permission_context):
    from alembic import command
    from alembic.config import Config
    from cloudops.shared.database import BACKEND_ROOT
    from uuid import UUID

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    identifier = UUID(permission_context.consumer.execution_id)
    with sessions()() as db:
        original = db.get(Execution, identifier)
        plan = db.get(ActionPlan, original.plan_id)
        before = copy.deepcopy((plan.content, plan.digest, original.result))
    try:
        command.downgrade(config, "0015_recovery_verification")
        command.upgrade(config, "head")
        with sessions()() as db, db.begin():
            original = db.get(Execution, identifier)
            plan = db.get(ActionPlan, original.plan_id)
            assert (plan.content, plan.digest, original.result) == before
            assert plan.forward_execution_id is None
            assert db.get(RollbackWindow, identifier) is None
            now = database_now(db)
            db.add(
                RollbackWindow(
                    forward_execution_id=identifier,
                    eligible_from=now,
                    deadline=now + timedelta(seconds=600),
                    policy_digest="a" * 64,
                )
            )
        with pytest.raises(RuntimeError, match="Retained inverse"):
            command.downgrade(config, "0015_recovery_verification")
        with sessions()() as db:
            assert db.get(RollbackWindow, identifier) is not None
    finally:
        command.upgrade(config, "head")


@pytest.mark.parametrize("template", ["set_memory_profile", "restore_deployment_template"])
def test_captured_template_inverse_needs_no_new_forward_selector(candidate, monkeypatch, template):
    from test_recovery_plans import memory_advice, live_baseline, propose as advice_plan
    from recovery_verification_fixtures import profile as controlled_profile
    from cloudops.features.recovery import plans
    from cloudops.features.recovery.fields import selected
    from cloudops.features.workloads.kubernetes import Kubernetes

    c = candidate
    container = c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]
    if template == "set_memory_profile":
        memory_advice(c)
    else:
        container["resources"]["limits"] = {"cpu": "100m", "memory": "64Mi"}
        c.recovery_body["baseline_id"] = live_baseline(c)
        container["image"] = "example/fixture:previous-safe"
        c.recovery_rules["qualified_images"].append(container["image"])
    before = selected(c.consumer.deployment)[0]
    desired = copy.deepcopy(before)
    if template == "set_memory_profile":
        p = c.recovery_rules["memory_profiles"][0]
        desired[0]["resources"]["requests"]["memory"] = p["request"]
        desired[0]["resources"]["limits"]["memory"] = p["limit"]
    else:
        desired[0]["image"] = "example/fixture:1"
    c.recovery_rules.update(
        inverse_policy="captured-old-state/v1",
        inverse_window_seconds=600,
        verification_policy="controlled-profile",
        inverse_qualified_states=[
            {
                "workload_uid": c.consumer.uid,
                "containers": before,
                "qualification_reference": "Controlled captured old-state fixture; no live quality claim",
            }
        ],
    )
    c.recovery_path.write_text(json.dumps(c.recovery_rules))
    profiles = []
    for version, containers in [("forward", desired), ("inverse", before)]:
        value = controlled_profile(template).model_dump(mode="json")
        value.update(
            version=version,
            workload_uid=c.consumer.uid,
            containers=containers,
            replicas=[1],
            image_ids={"consumer": "sha256:" + "b" * 64},
        )
        profiles.append(verification_policy.VerificationProfile.model_validate(value))

    def lookup(uid, name, state):
        return next((p for p in profiles if p.matches(uid, name, state)), None)

    monkeypatch.setattr(plans, "load_for", lookup)
    monkeypatch.setattr(rollback, "load_for", lookup)
    monkeypatch.setattr(verification_policy, "load_for", lookup)
    monkeypatch.setattr(Kubernetes, "capacity_budget", lambda *_args: None)
    forward = advice_plan(c)
    assert forward["content"]["execution_capability"] == "conditional_dispatch_v2"
    identifier = approve(c, forward)
    with sessions()() as db, db.begin():
        row = db.get(Execution, identifier)
        now = database_now(db)
        anchor = now - timedelta(seconds=301)
        row.dispatch_started_at = row.created_at = anchor - timedelta(seconds=1)
        row.result = service.receipt_result(
            {
                "schema_version": "RecoveryReceipt/v2",
                "operation_id": str(identifier),
                "plan_id": forward["id"],
                "plan_digest": forward["digest"],
                "attribution": "acknowledged",
                "code": "controlled_ack",
                "reason": "Controlled exact ACK and elapsed verification input for inverse derivation",
                "acknowledged_at": anchor.isoformat(),
                "acknowledged_resource_version": "controlled-rv",
            },
            identifier,
            row.dispatch_request,
        )
        record = Verification(
            execution_id=identifier,
            profile=profiles[0].model_dump(mode="json"),
            profile_digest=profiles[0].digest(),
            acknowledged_at=anchor,
            deadline=anchor + timedelta(seconds=300),
            samples={},
            result={},
        )
        db.add(record)
        verification.finalize(
            db,
            row,
            record,
            {
                "state": "passed",
                "fixture": "Controlled finalized input, not live application verification",
            },
        )
    container.update(image=desired[0]["image"], resources=copy.deepcopy(desired[0]["resources"]))
    c.recovery_rules["memory_profiles"] = []
    c.recovery_path.write_text(json.dumps(c.recovery_rules))

    def no_new_forward_selector(*args):
        raise AssertionError("An inverse must not select a new forward baseline or memory profile")

    monkeypatch.setattr(plans, "desired_fields", no_new_forward_selector)
    response = inverse(c, identifier)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["content"]["before"] == forward["content"]["desired"]
    assert result["content"]["desired"] == forward["content"]["before"]
    assert set(result["content"]["selector"]) == {"forward_operation_id", "forward_plan_id"}
    assert c.actors["operator"].client.get(f"/api/plans/{result['id']}").json()["valid"]
