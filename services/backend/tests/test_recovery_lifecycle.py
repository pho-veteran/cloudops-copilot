"""PostgreSQL API/job lifecycle with a controlled gateway; no model inference or live writes."""

import copy
import threading
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_permissions import permission_context as _permission_context
from recovery_gateway_fixtures import (
    gateway_case as _gateway_case,
    executor as _executor,
    approve as approve_gateway,
    intent,
)
from test_executor import maintain, ready

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.investigations.collector import Interrupted
from cloudops.features.recovery import service
from cloudops.features.recovery.classification import classify, classification_expression
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.outcomes import RECONCILE_ATTEMPTS, RECONCILE_JOB
from cloudops.features.recovery.reconciliation import ReconciliationHandler, reconcile
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, claim_next, finish

permission_context = _permission_context
gateway_case, executor = _gateway_case, _executor


def approve(c):
    actor = c.actors["operator"]
    path = f"/api/plans/{c.consumer.plan_id}"
    reviewed = actor.client.get(path).json()
    assert reviewed["valid"], reviewed
    response = actor.client.post(
        path + "/decision",
        headers=actor.headers,
        json={
            "version": reviewed["plan"]["version"],
            "digest": reviewed["plan"]["digest"],
            "review_token": reviewed["review_token"],
            "decision": "approve",
        },
    )
    assert response.status_code == 200, response.text
    with sessions()() as db:
        return db.scalar(select(Execution.id).where(Execution.plan_id == UUID(c.consumer.plan_id)))


def run_execution():
    handler = service.ExecutionHandler()
    claim = claim_next({"recovery_execution_v1": handler}, GATE)
    assert claim is not None
    result = service.execute(claim.subject_id, before_call=lambda: None)
    assert finish(claim, handler, result=result, gate=GATE)
    return claim, result


def test_actual_approval_freezes_request_and_ack_is_honestly_manual(
    permission_context, monkeypatch
):
    c = permission_context
    identifier = approve(c)
    with sessions()() as db:
        original = copy.deepcopy(db.get(Execution, identifier).dispatch_request)
    calls = []

    def gateway(path, body=None, **kwargs):
        calls.append((path, body))
        return {
            "outcome": "acknowledged",
            "verification": "inconclusive",
            "reason": "Controlled ACK only.",
        }

    monkeypatch.setattr(service.client, "gateway", gateway)
    _, result = run_execution()
    assert result["outcome"] == "acknowledged" and calls == [("/dispatch", original)]
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "manual_intervention" and not row.holds_lock
        assert row.dispatch_started_at is not None and row.dispatch_request == original
        assert row.outcome.attribution == "acknowledged"
        assert row.outcome.verification == "inconclusive"
        assert not db.scalar(select(Job.id).where(Job.kind == RECONCILE_JOB))


def test_after_call_admission_takeover_never_posts_again(permission_context, monkeypatch):
    identifier = approve(permission_context)
    claim = claim_next({"recovery_execution_v1": service.ExecutionHandler()}, GATE)
    calls = []
    count = 0

    def boundary():
        nonlocal count
        count += 1
        if count == 2:
            raise Interrupted()

    def gateway(path, body=None, **kwargs):
        calls.append(path)
        raise DomainError(404, "operation_not_found", "Controlled missing receipt.")

    monkeypatch.setattr(service.client, "gateway", gateway)
    with pytest.raises(Interrupted):
        service.execute(identifier, before_call=boundary)
    assert calls == []
    with sessions()() as db:
        row = db.get(Execution, identifier)
        deadline, request = row.reconcile_deadline, copy.deepcopy(row.dispatch_request)
        assert row.dispatch_started_at is not None
    result = service.execute(identifier, before_call=lambda: None)
    assert result["outcome"] == "outcome_unknown"
    assert calls == [f"/operations/{identifier}"]
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.reconcile_deadline == deadline and row.dispatch_request == request
    assert finish(claim, service.ExecutionHandler(), result=result, gate=GATE)


def test_unknown_matching_desired_exhausts_fixed_budget_and_retains_lock(
    permission_context, monkeypatch
):
    c = permission_context
    identifier = approve(c)
    calls = []

    def gateway(path, body=None, **kwargs):
        calls.append(path)
        raise DomainError(503, "executor_unavailable", "Controlled transport unavailable.")

    monkeypatch.setattr(service.client, "gateway", gateway)
    _, result = run_execution()
    assert result["outcome"] == "outcome_unknown"
    c.consumer.deployment["spec"]["replicas"] = 2
    with sessions()() as db:
        deadline = db.get(Execution, identifier).reconcile_deadline
    for number in range(1, RECONCILE_ATTEMPTS + 1):
        with sessions()() as db, db.begin():
            job = db.scalar(
                select(Job).where(Job.kind == RECONCILE_JOB, Job.subject_id == identifier)
            )
            job.due_at = database_now(db) - timedelta(seconds=1)
        claim = claim_next({RECONCILE_JOB: ReconciliationHandler()}, GATE)
        assert claim is not None
        reconcile(claim, GATE, threading.Event())
        with sessions()() as db:
            row = db.get(Execution, identifier)
            assert row.reconcile_attempts == number and row.reconcile_deadline == deadline
            assert row.result["reconciliation_observation"]["desired_fields_match"] is True
            assert row.holds_lock and row.outcome.attribution == "outcome_unknown"
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "outcome_unknown" and row.finished_at is not None
        assert db.scalar(select(Job).where(Job.kind == RECONCILE_JOB)).state == "completed"
    assert (
        calls.count("/dispatch") == 1
        and calls.count(f"/operations/{identifier}") == RECONCILE_ATTEMPTS
    )
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=actor.headers,
        json={
            "action": "resolve",
            "reason": "Hand off exhausted unknown; retain target quarantine.",
        },
    )
    assert response.status_code == 200, response.text
    with sessions()() as db:
        assert db.get(Execution, identifier).holds_lock


def test_resolve_retires_unclaimed_execution_but_claim_wins_conflict(permission_context):
    c = permission_context
    identifier = approve(c)
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=actor.headers,
        json={"action": "resolve", "reason": "Retire the unclaimed change before dispatch."},
    )
    assert response.status_code == 200, response.text
    assert claim_next({"recovery_execution_v1": service.ExecutionHandler()}, GATE) is None
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert not row.holds_lock and row.dispatch_started_at is None
        assert row.outcome.attribution == "not_sent"
        assert db.get(ActionPlan, row.plan_id).state == "invalidated"


def test_claimed_execution_cannot_be_hidden_by_resolve(permission_context):
    c = permission_context
    approve(c)
    assert claim_next({"recovery_execution_v1": service.ExecutionHandler()}, GATE) is not None
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=actor.headers,
        json={"action": "resolve", "reason": "Attempt resolution after claim."},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "execution_active"


@pytest.mark.parametrize("acknowledged", [True, False])
def test_root_future_authority_does_not_rewrite_attribution(gateway_case, acknowledged):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve_gateway(c, intent(c))
    c.provider.lose_ack = not acknowledged
    receipt = c.gate.dispatch(body)
    expected = "acknowledged" if acknowledged else "outcome_unknown"
    assert receipt["attribution"] == expected
    with sessions()() as db, db.begin():
        row = db.get(Execution, body.execution_id)
        row.state = "verifying" if acknowledged else "reconciling"
        row.result = {
            "outcome": expected,
            "attribution": expected,
            "verification": "pending" if acknowledged else "inconclusive",
            "gateway_receipt": receipt,
        }
    proof, _ = maintain(c.gate, c.uid, "enable_target")
    assert proof["status"] == "applied", proof
    with sessions()() as db, db.begin():
        row = db.get(Execution, body.execution_id)
        assert row.maintenance_released and not row.holds_lock
        assert row.outcome.attribution == expected and row.outcome.verification == "inconclusive"
        assert row.result["gateway_receipt"] == receipt
        expected_class = "terminal" if acknowledged else "terminal_unknown_released"
        assert (
            classify(row.state, row.holds_lock, row.maintenance_released, row.result)
            == expected_class
        )
        assert (
            db.scalar(
                select(
                    classification_expression(
                        Execution.state,
                        Execution.holds_lock,
                        Execution.maintenance_released,
                        Execution.result,
                    )
                ).where(Execution.id == row.id)
            )
            == expected_class
        )
        retained = copy.deepcopy(row.result)
        service.ExecutionHandler().publish(db, row.id, service.unknown("Late obsolete worker"))
        assert row.result == retained and not row.holds_lock


@pytest.mark.parametrize(
    "changed", ["operation_id", "plan_id", "plan_digest", "protocol", "ack_metadata"]
)
def test_receipt_must_match_exact_operation_and_plan(gateway_case, changed):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve_gateway(c, intent(c))
    receipt = c.gate.dispatch(body)
    assert (
        service.receipt_result(receipt, body.operation_id, body.model_dump(mode="json"))["outcome"]
        == "acknowledged"
    )
    bad = copy.deepcopy(receipt)
    if changed in {"operation_id", "plan_id"}:
        bad[changed] = str(uuid4())
    elif changed == "plan_digest":
        bad[changed] = "0" * 64
    elif changed == "ack_metadata":
        bad["acknowledged_at"] = None
    else:
        bad = {"outcome": "acknowledged"}
    with pytest.raises(ValueError):
        service.receipt_result(bad, body.operation_id, body.model_dump(mode="json"))


@pytest.mark.parametrize("exhaustion", ["deadline", "attempts"])
def test_takeover_cannot_reset_a_persisted_receipt_read_budget(
    permission_context, monkeypatch, exhaustion
):
    identifier = approve(permission_context)
    claim = claim_next({"recovery_execution_v1": service.ExecutionHandler()}, GATE)
    count = 0

    def boundary():
        nonlocal count
        count += 1
        if count == 2:
            raise Interrupted()

    with pytest.raises(Interrupted):
        service.execute(identifier, before_call=boundary)
    with sessions()() as db, db.begin():
        row = db.get(Execution, identifier)
        if exhaustion == "deadline":
            row.reconcile_deadline = database_now(db) - timedelta(seconds=1)
        else:
            row.reconcile_attempts = RECONCILE_ATTEMPTS
    calls = []
    monkeypatch.setattr(service.client, "gateway", lambda *args, **kwargs: calls.append(args))
    result = service.execute(identifier, before_call=lambda: None)
    assert result["outcome"] == "outcome_unknown" and calls == []
    assert finish(claim, service.ExecutionHandler(), result=result, gate=GATE)
    if exhaustion == "attempts":
        assert claim_next({RECONCILE_JOB: ReconciliationHandler()}, GATE) is None
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.holds_lock and row.state == "outcome_unknown" and row.finished_at is not None


def test_later_exact_ack_finishes_reconciliation_without_reposting(permission_context, monkeypatch):
    identifier = approve(permission_context)
    calls = []

    def gateway(path, body=None, **kwargs):
        calls.append(path)
        if path == "/dispatch":
            raise DomainError(503, "executor_unavailable", "Controlled lost acknowledgement.")
        return {
            "outcome": "acknowledged",
            "verification": "inconclusive",
            "reason": "Exact durable ACK found later.",
        }

    monkeypatch.setattr(service.client, "gateway", gateway)
    run_execution()
    claim = claim_next({RECONCILE_JOB: ReconciliationHandler()}, GATE)
    assert claim is not None
    reconcile(claim, GATE, threading.Event())
    assert calls == ["/dispatch", f"/operations/{identifier}"]
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "manual_intervention" and not row.holds_lock
        assert (
            row.outcome.attribution == "acknowledged" and row.outcome.verification == "inconclusive"
        )
        assert db.get(Job, claim.id).state == "completed"


def test_root_target_summary_includes_allowlisted_uid_absent_from_postgres(gateway_case):
    from cloudops.features.recovery.gateway import Gateway
    from cloudops.features.workloads.models import Workload

    c = gateway_case
    entries = [vars(value) for value in c.gate.workloads.values()]
    absent = {**entries[0], "uid": str(uuid4()), "name": "configured-but-absent"}
    gate = Gateway(
        c.gate.root,
        kube=c.provider,
        allowlist=[*entries, absent],
        identity_file=c.gate.identity_file,
    )
    before = [gate.target(entry["uid"])["epoch"] for entry in [*entries, absent]]
    result, _ = maintain(gate, c.uid, "target_status")
    assert {item["uid"] for item in result["targets"]} == {c.uid, absent["uid"]}
    assert all(not item["enabled"] and item["recovery_mode"] for item in result["targets"])
    assert all(
        set(item) == {"deployment_id", "uid", "epoch", "enabled", "recovery_mode"}
        for item in result["targets"]
    )
    assert [gate.target(entry["uid"])["epoch"] for entry in [*entries, absent]] == before
    assert c.provider.writes == 0
    with sessions()() as db:
        assert db.scalar(select(Workload.id).where(Workload.uid == absent["uid"])) is None
