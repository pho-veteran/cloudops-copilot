"""Real admission, resolution and job claim contend without widening lock order."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

from conftest import sign_in
from sqlalchemy import select, text
from test_access_admission import create, incident_body, rerun

from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.worker import HANDLERS
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recovery.models import ActionPlan
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, claim_next


def test_resolve_new_run_and_claim_have_one_consistent_outcome(client):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    iid, old_run = UUID(created["incident_id"]), UUID(created["run_id"])
    with sessions()() as db, db.begin():
        incident = db.get(Incident, iid)
        db.get(InvestigationRun, old_run).state = "completed"
        db.scalar(select(Job).where(Job.subject_id == old_run)).state = "completed"
        plan = ActionPlan(
            incident_id=iid,
            workload_id=incident.workload_id,
            content={"fixture": "draft"},
            digest="0" * 64,
            target_epoch="fixture",
            rationale="Draft before race",
            created_by=db.scalar(text("SELECT id FROM users WHERE username='operator'")),
            expires_at=database_now(db) + timedelta(minutes=5),
        )
        db.add(plan)
        db.flush()
        plan_id = plan.id
    start = Barrier(3)

    def contend(fn):
        start.wait(timeout=10)
        return fn()

    with ThreadPoolExecutor(max_workers=3) as pool:
        resolve = pool.submit(
            contend,
            lambda: client.post(
                f"/api/incidents/{iid}/lifecycle",
                headers=headers,
                json={"action": "resolve", "reason": "Concurrent handoff"},
            ),
        )
        admit = pool.submit(contend, lambda: rerun(client, headers, iid))
        claim = pool.submit(contend, lambda: claim_next(HANDLERS, GATE))
        resolved, admitted, claimed = (
            resolve.result(timeout=20),
            admit.result(timeout=20),
            claim.result(timeout=20),
        )
    assert resolved.status_code == 200, resolved.text
    assert admitted.status_code in (201, 409), admitted.text
    with sessions()() as db:
        assert db.get(Incident, iid).state == "resolved"
        assert db.get(ActionPlan, plan_id).state == "invalidated"
        if admitted.status_code == 201:
            run = db.get(InvestigationRun, UUID(admitted.json()["run_id"]))
            job = db.scalar(select(Job).where(Job.subject_id == run.id))
            if run.state == "cancelled":
                assert run.outcome_code == "incident_resolved"
                assert job is None or job.state != "queued"
                assert claimed is None or claimed.subject_id != run.id
            else:
                assert run.state == "collecting"
                assert claimed is not None and claimed.subject_id == run.id
                assert job.state == "running"
        else:
            assert claimed is None
