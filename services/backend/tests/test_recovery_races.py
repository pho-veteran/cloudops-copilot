"""Deterministic transaction races: the claim lock or the domain invalidation wins once."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_access_admission import rerun
from test_recovery_lifecycle import approve
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.recovery import service
from cloudops.features.recovery.models import Execution
from cloudops.shared.database import sessions
from cloudops.shared.jobs import Job, claim_next

permission_context = _permission_context


@pytest.mark.parametrize("action", ["resolve", "reinvestigate"])
@pytest.mark.parametrize("winner", ["claim", "invalidation"])
def test_claim_and_domain_invalidation_have_one_winner(
    permission_context, monkeypatch, action, winner
):
    c = permission_context
    identifier = approve(c)
    actor = c.actors["operator"]
    entered, release = threading.Event(), threading.Event()
    handler = service.ExecutionHandler()

    def invalidate():
        if action == "reinvestigate":
            return rerun(actor.client, actor.headers, c.consumer.incident_id)
        return actor.client.post(
            f"/api/incidents/{c.consumer.incident_id}/lifecycle",
            headers=actor.headers,
            json={"action": "resolve", "reason": "Race against the exact recovery claim."},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            if winner == "claim":
                original = handler.eligible

                def eligible(db, subject):
                    entered.set()
                    assert release.wait(5)
                    return original(db, subject)

                monkeypatch.setattr(handler, "eligible", eligible)
                future = pool.submit(claim_next, {"recovery_execution_v1": handler}, GATE)
                assert entered.wait(5)
                response = invalidate()
                assert response.status_code == 409, response.text
                assert response.json()["error"]["code"] == "execution_active"
                release.set()
                assert future.result(timeout=5).subject_id == identifier
            else:
                original = service.retire_queued

                def retire(*args):
                    value = original(*args)
                    assert value
                    entered.set()
                    assert release.wait(5)
                    return value

                monkeypatch.setattr(service, "retire_queued", retire)
                future = pool.submit(invalidate)
                assert entered.wait(5)
                assert claim_next({"recovery_execution_v1": handler}, GATE) is None
                release.set()
                response = future.result(timeout=5)
                assert response.status_code == (200 if action == "resolve" else 201), response.text
        finally:
            release.set()
    with sessions()() as db:
        row = db.get(Execution, identifier)
        job = db.scalar(
            select(Job).where(Job.kind == "recovery_execution_v1", Job.subject_id == identifier)
        )
        if winner == "claim":
            assert row.state == "executing" and row.holds_lock and job.state == "running"
        else:
            assert row.outcome.attribution == "not_sent" and not row.holds_lock
            assert row.dispatch_started_at is None and job.state == "cancelled"
