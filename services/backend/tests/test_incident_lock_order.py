"""Real transaction and consumer-guard evidence for bounded incident admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
import time
from uuid import UUID

import pytest
from conftest import sign_in
from sqlalchemy import func, select, text
from test_access_admission import create, incident_body, rerun
from test_incident_requests import (
    automatic,
    capacity as _capacity,
    identities as _identities,
    free_capacity,
)
from test_consumers import decide
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.worker import HANDLERS
from cloudops.features.incidents import lifecycle
from cloudops.features.incidents.models import Incident
from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.investigations.promotion import promote_deferred
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recovery import client as gateway_client
from cloudops.features.recovery import service as recovery
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.recovery.classification import BLOCKING, classify
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, engine, sessions
from cloudops.shared.jobs import Job, JobAttempt, claim_next

permission_context = _permission_context
capacity = _capacity
identities = _identities


def test_resolve_and_claim_with_a_proposed_plan_do_not_deadlock(client, monkeypatch):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    incident_id, run_id = UUID(ids["incident_id"]), UUID(ids["run_id"])
    with sessions()() as db, db.begin():
        incident = db.get(Incident, incident_id)
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        plan = ActionPlan(
            incident_id=incident_id,
            workload_id=incident.workload_id,
            content={"source": "draft_fixture"},
            digest="0" * 64,
            target_epoch="fixture",
            rationale="Draft proposed before collection",
            created_by=actor,
            expires_at=database_now(db) + timedelta(minutes=5),
        )
        db.add(plan)
        db.flush()
        plan_id = plan.id

    claim_holds_run = Event()
    resolve_reached_runs = Event()
    invalidate = recovery.invalidate
    cancel_pending = lifecycle.cancel_pending

    def pause_claim_before_plans(db, target):
        # eligible() already holds this Run; release it only after resolve reaches
        # its Run cancellation boundary. A plan-first resolver creates a real cycle.
        claim_holds_run.set()
        assert resolve_reached_runs.wait(10), "resolve never reached its Run boundary"
        return invalidate(db, target)

    def observe_resolve_run_boundary(*args, **kwargs):
        resolve_reached_runs.set()
        return cancel_pending(*args, **kwargs)

    monkeypatch.setattr(recovery, "invalidate", pause_claim_before_plans)
    monkeypatch.setattr(lifecycle, "cancel_pending", observe_resolve_run_boundary)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(claim_next, HANDLERS)
        assert claim_holds_run.wait(10), "claim never acquired its Run"
        resolving = pool.submit(
            client.post,
            f"/api/incidents/{incident_id}/lifecycle",
            json={"action": "resolve", "reason": "Recovered while collection was claiming"},
            headers=headers,
        )
        claim = claiming.result(timeout=20)
        response = resolving.result(timeout=20)

    assert claim is not None and claim.subject_id == run_id
    assert response.status_code == 200
    with sessions()() as db:
        assert db.get(Incident, incident_id).state == "resolved"
        assert db.get(InvestigationRun, run_id).state == "collecting"
        assert db.scalar(select(ActionPlan.state).where(ActionPlan.id == plan_id)) == "invalidated"


def test_approval_live_probe_holds_no_admission_or_incident_lock(permission_context, monkeypatch):
    context = permission_context
    target = gateway_client.target
    observed = []

    def checked_target(uid):
        with sessions()() as probe, probe.begin():
            assert probe.scalar(text("SELECT pg_try_advisory_xact_lock(742001)")), (
                "approval held the global admission lock across a live request"
            )
            probe.execute(
                select(Incident.id)
                .where(Incident.id == UUID(context.consumer.incident_id))
                .with_for_update(nowait=True)
            ).all()
        observed.append(uid)
        return target(uid)

    monkeypatch.setattr(gateway_client, "target", checked_target)
    response = decide(context)
    assert response.status_code == 200, response.text
    assert observed


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("resolve", 409, "plan_unavailable"),
        ("retire", 422, "workload_unavailable"),
        ("consume", 409, "plan_refetch_required"),
    ],
)
def test_approval_rechecks_committed_state_after_live_probe(
    permission_context, monkeypatch, change, status, code
):
    context = permission_context
    target = gateway_client.target
    with sessions()() as db:
        before = tuple(
            db.scalar(select(func.count()).select_from(model)) for model in (Job, Execution)
        )

    def changed_during_probe(uid):
        authority = target(uid)
        with sessions()() as db, db.begin():
            if change == "resolve":
                db.get(Incident, UUID(context.consumer.incident_id)).state = "resolved"
            elif change == "retire":
                db.get(Workload, UUID(context.consumer.workload_id)).active = False
            else:
                review = db.scalar(
                    select(PlanReview).where(PlanReview.plan_id == UUID(context.consumer.plan_id))
                )
                review.consumed = True
        return authority

    monkeypatch.setattr(gateway_client, "target", changed_during_probe)
    response = decide(context)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    with sessions()() as db:
        after = tuple(
            db.scalar(select(func.count()).select_from(model)) for model in (Job, Execution)
        )
        assert after == before
        assert db.get(ActionPlan, UUID(context.consumer.plan_id)).state == "proposed"


# Future states are producer-contract fixtures, not evidence of live E07 engines.
EXECUTION_CASES = [
    ("queued", True, False, True),
    ("executing", True, False, True),
    ("verifying", True, False, True),
    ("reconciling", True, False, True),
    ("outcome_unknown", True, False, False),
    ("outcome_unknown", False, True, False),
    ("outcome_unknown", True, True, True),
    ("outcome_unknown", False, False, True),
    ("manual_intervention", False, True, True),
    ("future_unrecognized", False, False, True),
    ("manual_intervention", True, False, True),
]


@pytest.mark.parametrize(("state", "holds_lock", "released", "blocked"), EXECUTION_CASES)
@pytest.mark.parametrize("boundary", ["admit", "promote", "claim", "resolve"])
def test_shared_execution_fixtures_reach_real_consumer_guards(
    client, state, holds_lock, released, blocked, boundary
):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    incident_id, run_id = UUID(ids["incident_id"]), UUID(ids["run_id"])
    with sessions()() as db, db.begin():
        incident = db.get(Incident, incident_id)
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        run = db.get(InvestigationRun, run_id)
        job = db.scalar(select(Job).where(Job.subject_id == run_id))
        if boundary == "admit":
            run.state, job.state = "insufficient_evidence", "completed"
        elif boundary == "promote":
            db.delete(job)
            run.queue_reason = "capacity"
        plan = ActionPlan(
            incident_id=incident_id,
            workload_id=incident.workload_id,
            state="approved",
            content={"source": "execution_contract_fixture"},
            digest="0" * 64,
            target_epoch="fixture",
            rationale="Persisted consumer-state fixture; future engines are not implemented",
            created_by=actor,
            expires_at=database_now(db) + timedelta(minutes=5),
        )
        db.add(plan)
        db.flush()
        execution = Execution(
            plan_id=plan.id,
            workload_id=incident.workload_id,
            state=state,
            holds_lock=holds_lock,
            maintenance_released=released,
            result={
                "outcome": "outcome_unknown",
                "maintenance_request_id": "00000000-0000-4000-8000-000000000001",
            }
            if state == "outcome_unknown"
            else {},
        )
        db.add(execution)
        db.flush()
        execution_id = execution.id
        if state == "outcome_unknown":
            # Terminal producer-contract fixture; no live E07 future-state engine is implied.
            db.add(Job(kind="recovery_execution_v1", subject_id=execution_id, state="completed"))
    assert (classify(state, holds_lock, released) in BLOCKING) == blocked
    if boundary == "admit":
        response = rerun(client, headers, incident_id)
        assert response.status_code == (409 if blocked else 201), response.text
        if blocked:
            assert response.json()["error"]["code"] == "execution_active"
    elif boundary == "promote":
        assert promote_deferred() == (0 if blocked else 1)
    elif boundary == "claim":
        claim = claim_next(HANDLERS)
        assert (claim is None) == blocked
        if blocked:
            with sessions()() as db:
                assert db.scalar(select(func.count()).select_from(JobAttempt)) == 0
    else:
        response = client.post(
            f"/api/incidents/{incident_id}/lifecycle",
            json={"action": "resolve", "reason": "Consumer-contract fixture"},
            headers=headers,
        )
        assert response.status_code == (409 if blocked else 200), response.text
        if blocked:
            assert response.json()["error"]["code"] == "execution_active"
    with sessions()() as db:
        assert db.get(Execution, execution_id).holds_lock == holds_lock
        assert db.get(Execution, execution_id).maintenance_released == released
        if boundary in {"promote", "claim"} and blocked:
            assert db.get(InvestigationRun, run_id).state == "cancelled"


@pytest.mark.parametrize(
    "state", ["queued", "collecting", "analyzing", "waiting_for_quota", "future_unknown"]
)
def test_approval_refuses_every_nonterminal_run_before_live_reads(
    permission_context, monkeypatch, state
):
    context = permission_context
    with sessions()() as db, db.begin():
        actor = UUID(context.actors["operator"].session["principal"]["id"])
        db.add(
            InvestigationRun(
                incident_id=UUID(context.consumer.incident_id), requested_by=actor, state=state
            )
        )

    def no_network(_uid):
        pytest.fail("blocked approval attempted a live read")

    monkeypatch.setattr(gateway_client, "target", no_network)
    response = decide(context)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "run_active"
    with sessions()() as db:
        review = db.scalar(
            select(PlanReview).where(PlanReview.plan_id == UUID(context.consumer.plan_id))
        )
        assert not review.consumed
        assert db.get(ActionPlan, UUID(context.consumer.plan_id)).state == "proposed"


def test_manual_automatic_admission_and_promotion_contend_with_available_slots(
    client, capacity, identities
):
    headers = sign_in(client)
    body = incident_body(client)
    capacity(jobs=2, deferred=1, filled=2)
    deferred = automatic(identities, "initial-deferred", body["workload_id"])
    free_capacity()
    ready = Barrier(3)

    def contend(action):
        ready.wait(timeout=10)
        return action()

    with ThreadPoolExecutor(max_workers=3) as pool, sessions()() as holder:
        holder.begin()
        lock_admission(holder)
        try:
            promoting = pool.submit(contend, promote_deferred)
            incoming = pool.submit(
                contend, lambda: automatic(identities, "new-automatic", body["workload_id"])
            )
            manual = pool.submit(contend, lambda: create(client, headers, body))
            deadline = time.monotonic() + 10
            # pg_stat_activity's cached snapshot must refresh between polls.
            with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
                while (
                    monitor.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND wait_event='advisory'"
                        )
                    ).scalar()
                    < 3
                ):
                    assert time.monotonic() < deadline, "contenders did not overlap at admission"
                    time.sleep(0.02)
            holder.commit()
            assert promoting.result(timeout=15) in (0, 1)
            assert incoming.result(timeout=15).run_id != deferred.run_id
            assert manual.result(timeout=15).status_code in (201, 409)
        finally:
            holder.rollback()
    with sessions()() as db:
        active = db.scalar(
            select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))
        )
        waiting = db.scalar(
            select(func.count())
            .select_from(InvestigationRun)
            .where(InvestigationRun.queue_reason == "capacity")
        )
        assert active == 2
        assert waiting <= 1
        assert db.get(InvestigationRun, deferred.run_id) is not None


@pytest.mark.parametrize(("state", "holds_lock", "released", "blocked"), EXECUTION_CASES)
def test_approval_respects_classification_and_retained_target_lock(
    permission_context, state, holds_lock, released, blocked
):
    context = permission_context
    with sessions()() as db, db.begin():
        execution = db.get(Execution, UUID(context.consumer.execution_id))
        execution.state = state
        execution.holds_lock = holds_lock
        execution.maintenance_released = released
        # Isolate classification from the independent short target cooldown.
        execution.created_at = database_now(db) - timedelta(seconds=61)
    response = decide(context)
    denied = blocked or holds_lock
    assert response.status_code == (409 if denied else 200), response.text
    if denied:
        assert response.json()["error"]["code"] == "target_locked"
    with sessions()() as db:
        execution = db.get(Execution, UUID(context.consumer.execution_id))
        assert execution.holds_lock == holds_lock and execution.maintenance_released == released
