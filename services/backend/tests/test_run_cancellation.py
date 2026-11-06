"""Honest cancellation: queued and declared no-call waits only; started work is never 'aborted'.

waiting_for_quota rows below are DB-seeded E06 contract fixtures (label: e06_contract_fixture).
No quota producer exists; these exercise the actual API and guards, not a live provider.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import checkpoint_adapters as adapters
import pytest
from conftest import sign_in
from sqlalchemy import select, update
from test_access_admission import create, incident_body
from test_collection_checkpoints import bound, context_for  # noqa: F401

from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.worker import HANDLERS
from cloudops.features.investigations.collector import collect_checkpointed
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import sessions
from cloudops.shared.jobs import Job, checkpoint, claim_next, finish


def cancel(client, headers, ids):
    return client.post(
        f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/cancel",
        headers=headers,
        json={},
    )


def seed_quota_wait(ids, provider_call):
    """e06_contract_fixture: a declared waiting_for_quota boundary with its Job unclaimed."""
    with sessions()() as db, db.begin():
        db.execute(
            update(InvestigationRun)
            .where(InvestigationRun.id == UUID(ids["run_id"]))
            .values(state="waiting_for_quota", provider_call=provider_call)
        )


def test_waiting_for_quota_without_a_provider_call_is_cancellable(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seed_quota_wait(ids, "none")
    view = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert view["cancel"] == {"allowed": True, "reason": "waiting_without_provider_call"}
    response = cancel(client, headers, ids)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["state"], body["outcome_code"]) == ("cancelled", "operator_cancelled")
    assert body["cancel"] == {"allowed": False, "reason": "already_cancelled"}
    with sessions()() as db:
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        assert run.provider_call is None
        assert db.scalar(select(Job.state).where(Job.subject_id == run.id)) == "cancelled"
    assert claim_next(HANDLERS, GATE) is None
    activity = client.get(f"/api/incidents/{ids['incident_id']}/activity").json()["items"]
    assert any(
        a["text"] == "Cancelled an investigation waiting for analysis quota." for a in activity
    )


@pytest.mark.parametrize("provider_call", ["in_flight", None])
def test_waiting_for_quota_with_a_call_or_undeclared_boundary_is_not_cancellable(
    client, provider_call
):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seed_quota_wait(ids, provider_call)
    response = cancel(client, headers, ids)
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "run_not_cancellable",
        "message": "A provider call is already in flight; it cannot be withdrawn.",
    }
    with sessions()() as db:
        assert db.get(InvestigationRun, UUID(ids["run_id"])).state == "waiting_for_quota"


def test_viewer_cannot_cancel_and_sees_the_same_boundary(client):
    ids = create(client, sign_in(client), incident_body(client)).json()
    viewer = sign_in(client, "viewer")
    response = cancel(client, viewer, ids)
    assert response.status_code == 403
    view = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert view["state"] == "queued" and view["cancel"]["allowed"]


def test_cancel_and_claim_race_has_one_honest_winner(client):
    headers = sign_in(client)
    outcomes = set()
    for _ in range(4):
        ids = create(client, headers, incident_body(client)).json()
        start = threading.Barrier(2)

        def contend(fn):
            start.wait(timeout=10)
            return fn()

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled = pool.submit(contend, lambda: cancel(client, headers, ids))
            claimed = pool.submit(contend, lambda: claim_next(HANDLERS, GATE))
            response, claim = cancelled.result(timeout=20), claimed.result(timeout=20)
        with sessions()() as db:
            run = db.get(InvestigationRun, UUID(ids["run_id"]))
        if response.status_code == 200:
            assert claim is None and run.state == "cancelled"
            outcomes.add("cancelled")
        else:
            assert response.status_code == 409 and claim is not None
            assert response.json()["error"]["message"].startswith("Collection already started")
            assert run.state == "collecting"
            assert finish(claim, HANDLERS[claim.kind], error="test cleanup", gate=GATE)
            with sessions()() as db, db.begin():
                db.execute(
                    update(InvestigationRun)
                    .where(InvestigationRun.id == run.id)
                    .values(state="failed", outcome_code="collection_failed")
                )
                db.execute(update(Job).where(Job.subject_id == run.id).values(state="failed"))
            outcomes.add("claimed")
    assert outcomes <= {"cancelled", "claimed"} and outcomes
    # A cancelled Run never resurrects: nothing remains claimable.
    assert claim_next(HANDLERS, GATE) is None


def test_resolve_during_a_checkpoint_neither_deadlocks_nor_aborts_collection(client, bound):  # noqa: F811
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    claim = claim_next(HANDLERS, GATE)
    context = context_for(claim)
    inside = threading.Event()
    result = adapters.state(context, 5)

    def slow_write(db, carry):
        from cloudops.features.investigations.storage import store_source

        inside.set()
        time.sleep(0.5)  # Holds the Job lock; resolve takes Incident -> Run -> queued Jobs.
        return store_source(db, claim.subject_id, context.workload_uid, result, carry)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(
            checkpoint, claim, "collect_source:kubernetes.state", GATE, slow_write
        )
        assert inside.wait(5)
        resolved = client.post(
            f"/api/incidents/{ids['incident_id']}/lifecycle",
            headers=headers,
            json={"action": "resolve", "reason": "Recovered during collection"},
        )
        assert writing.result(timeout=20)
    assert resolved.status_code == 200, resolved.text
    final = collect_checkpointed(claim, context, bound, GATE)
    assert finish(claim, HANDLERS[claim.kind], result=final, gate=GATE)
    view = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert view["state"] == "insufficient_evidence"
    # Resolved incidents do not gain a latest pin; reopening pins it as before.
    assert not any(row["pinned"] for row in view["evidence"])
