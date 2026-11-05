"""Issue #35: request windows, legacy replay, bounded deferral, promotion and cancellation."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import sign_in
from sqlalchemy import Column, MetaData, String, Table, Uuid, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_access_admission import create, incident_body, rerun
from test_consumers import decide
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.incidents.models import Admission, Incident, IncidentActivity
from cloudops.features.incidents.service import AutomaticRequest, admit_automatic
from cloudops.features.investigations import admission_settings, promotion
from cloudops.features.investigations.collector import CollectionResult
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.promotion import promote_deferred
from test_jobs import collect_claim
from cloudops.features.investigations.service import Origin
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.workloads.models import Workload
from cloudops.shared.config import settings
from cloudops.shared.database import BACKEND_ROOT, database_now, engine, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt, claim_next, finish

permission_context = _permission_context
LEGACY_CREATE = "24f358c29eecf592d7f87189f0321514d39b1c014660884c832caee098fb420b"


def committed_signature(body=None, incident_id=None):
    """The exact pre-#35 formula, kept here as an independent oracle."""
    return hashlib.sha256(
        json.dumps(
            {"body": body, "incident_id": str(incident_id) if incident_id else None},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def count(model, *where):
    with sessions()() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def run_row(run_id):
    with sessions()() as db:
        return db.get(InvestigationRun, UUID(str(run_id)))


def cancel(client, headers, incident_id, run_id, **body):
    return client.post(
        f"/api/incidents/{incident_id}/runs/{run_id}/cancel", json=body or None, headers=headers
    )


@pytest.fixture
def capacity(monkeypatch):
    """Configure small J/D bounds and fill J with unclaimable placeholder jobs."""
    original = settings()

    def configure(jobs: int, deferred: int, filled: int = 0):
        monkeypatch.setattr(promotion, "settings", lambda: replace(original, queue_limit=jobs))
        monkeypatch.setattr(admission_settings, "deferred_run_limit", lambda: deferred)
        with sessions()() as db, db.begin():
            db.add_all(Job(kind="capacity_fixture", subject_id=uuid4()) for _ in range(filled))

    return configure


def free_capacity():
    with sessions()() as db, db.begin():
        db.execute(update(Job).where(Job.kind == "capacity_fixture").values(state="completed"))


@pytest.fixture
def identities():
    """A caller-owned automatic identity store; the real episode store belongs to intake."""
    table = Table(
        "test_automatic_identity",
        MetaData(),
        Column("key", String(64), primary_key=True),
        Column("incident_id", Uuid, nullable=False),
        Column("run_id", Uuid, nullable=False),
    )
    table.create(engine(), checkfirst=True)
    yield table
    table.drop(engine())


def automatic(table, key, workload_id, *, fail_bind=False):
    def lookup(db):
        row = db.execute(select(table).where(table.c.key == key)).first()
        return (row.incident_id, row.run_id) if row else None

    def bind(db, view):
        db.execute(insert(table).values(key=key, incident_id=view.incident_id, run_id=view.run_id))
        if fail_bind:
            raise RuntimeError("caller storage failed after binding")

    with sessions()() as db, db.begin():
        return admit_automatic(
            db,
            Origin.alertmanager("test-alertmanager"),
            AutomaticRequest(UUID(workload_id), "Automatic fixture", "", "Why is it failing?"),
            lookup=lookup,
            bind=bind,
        )


def test_signatures_match_the_committed_formula():
    from cloudops.features.incidents.schemas import ManualIncidentRequest
    from cloudops.features.incidents.service import (
        legacy_create_signature,
        legacy_run_signature,
    )

    workload = UUID("00000000-0000-4000-8000-000000000001")
    incident = UUID("00000000-0000-4000-8000-000000000002")
    body = ManualIncidentRequest(workload_id=workload, title="Memory issue")
    assert legacy_create_signature(body) == LEGACY_CREATE
    human = ManualIncidentRequest(
        workload_id=workload, title="Memory issue", description="Giữ nguyên mô tả của người dùng."
    )
    assert legacy_create_signature(human) == committed_signature(
        {"workload_id": str(workload), "title": human.title, "description": human.description}
    )
    assert legacy_run_signature(incident) == committed_signature(incident_id=incident)
    assert legacy_run_signature(incident) == (
        "198e8e5145d478e147a7ef683cbf50b0b9daa492eb3215623a765648e8873138"
    )


def test_windows_resolve_once_and_invalid_windows_leave_no_work(client):
    headers = sign_in(client)
    body = incident_body(client)
    key = uuid4()
    first = create(client, headers, {**body, "question": "Why?", "window": {"preset": 15}}, key)
    assert first.status_code == 201, first.text
    window = first.json()["window"]
    assert window["source"] == "preset_15"
    span = datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])
    assert span == timedelta(minutes=15)
    time.sleep(1)
    replay = create(client, headers, {**body, "question": "Why?", "window": {"preset": 15}}, key)
    assert replay.json() == first.json()
    assert create(client, headers, {**body, "question": "Why?"}, key).status_code == 409

    with sessions()() as db:
        run = db.get(InvestigationRun, UUID(first.json()["run_id"]))
        assert (run.question, run.question_source) == ("Why?", "operator")
        assert db.scalar(select(Admission.signature_version)) == 2
    incidents = count(Incident)
    now = datetime.now(timezone.utc)
    invalid = [
        {
            "start": (now - timedelta(minutes=10)).isoformat(),
            "end": (now + timedelta(minutes=10)).isoformat(),
        },
        {
            "start": (now - timedelta(minutes=90)).isoformat(),
            "end": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "start": (now - timedelta(minutes=5, seconds=30)).isoformat(),
            "end": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "start": (now - timedelta(minutes=5)).isoformat(),
            "end": (now - timedelta(minutes=10)).isoformat(),
        },
    ]
    for window in invalid:
        response = create(client, headers, {**body, "question": "Why?", "window": window})
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_window"
    naive = {"start": "2026-11-01T00:00:00", "end": "2026-11-01T00:30:00"}
    assert create(client, headers, {**body, "window": naive}).status_code == 422
    assert create(client, headers, {**body, "window": {"preset": 45}}).status_code == 422
    assert count(Incident) == incidents


def test_explicit_window_is_normalized_and_attach_preserves_history(client):
    headers = sign_in(client)
    body = incident_body(client)
    first = create(client, headers, body).json()
    assert first["window"]["source"] == "legacy_default"
    with sessions()() as db:
        run = db.get(InvestigationRun, UUID(first["run_id"]))
        assert (run.question, run.question_source) == (body["title"], "title_default")
        assert db.scalar(select(Admission.signature_version)) == 1
    free = cancel(client, headers, first["incident_id"], first["run_id"])
    assert free.status_code == 200 and free.json()["state"] == "cancelled"

    end = datetime.now(timezone(timedelta(hours=7))) - timedelta(minutes=3)
    window = {"start": (end - timedelta(minutes=20)).isoformat(), "end": end.isoformat()}
    key = uuid4()
    attached = rerun(client, headers, first["incident_id"], key, window=window)
    assert attached.status_code == 201, attached.text
    value = attached.json()["window"]
    assert value["source"] == "explicit"
    assert datetime.fromisoformat(value["end"]) == end
    assert datetime.fromisoformat(value["end"]).utcoffset() == timedelta(0)
    # The same instant in another offset is the same canonical request.
    utc = {
        k: datetime.fromisoformat(v).astimezone(timezone.utc).isoformat() for k, v in window.items()
    }
    assert rerun(client, headers, first["incident_id"], key, window=utc).json() == attached.json()
    assert rerun(client, headers, first["incident_id"], key, reason="Different").status_code == 409
    detail = client.get(f"/api/incidents/{first['incident_id']}").json()
    assert detail["description"] == body["description"] and detail["origin"]["kind"] == "operator"
    runs = client.get(f"/api/incidents/{first['incident_id']}/runs").json()["items"]
    assert [r["id"] for r in runs] == [attached.json()["run_id"], first["run_id"]]
    assert runs[0]["question"] == "Check the workload again"


def test_attach_refusals_leave_no_partial_work(client, db):
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()
    incident = first["incident_id"]
    before = (count(InvestigationRun), count(Job), count(Admission))

    def code(response):
        return response.status_code, response.json()["error"]["code"]

    assert code(rerun(client, headers, incident)) == (409, "run_active")
    assert code(rerun(client, headers, incident, workload_id=str(uuid4()))) == (
        409,
        "target_mismatch",
    )
    empty = client.post(
        f"/api/incidents/{incident}/runs", headers={**headers, "Idempotency-Key": str(uuid4())}
    )
    assert code(empty) == (422, "run_reason_required")
    assert cancel(client, headers, incident, first["run_id"]).status_code == 200
    resolved = client.post(
        f"/api/incidents/{incident}/lifecycle",
        json={"action": "resolve", "reason": "Fixed"},
        headers=headers,
    )
    assert resolved.status_code == 200
    assert code(rerun(client, headers, incident)) == (409, "incident_resolved")
    with sessions()() as check, check.begin():
        workload = check.get(Incident, UUID(incident)).workload_id
        check.execute(update(Incident).where(Incident.id == UUID(incident)).values(state="open"))
        check.get(Workload, workload).active = False
    assert code(rerun(client, headers, incident)) == (422, "workload_unavailable")
    assert (count(InvestigationRun), count(Job), count(Admission)) == before


def test_committed_legacy_requests_replay_after_upgrade(client):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with sessions()() as db:
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        workload = db.scalar(text("SELECT id FROM workloads WHERE active ORDER BY id LIMIT 1"))
    command.downgrade(config, "0005_workload_observations")
    incident, first, second, create_key, run_key = [uuid4() for _ in range(5)]
    body = {"workload_id": str(workload), "title": "Prior incident", "description": "Giữ nguyên"}
    with engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO incidents (id,workload_id,title,description,state,created_by) "
                "VALUES (:id,:workload,'Prior incident','Giữ nguyên','open',:actor)"
            ),
            {"id": incident, "workload": workload, "actor": actor},
        )
        for run in (first, second):
            db.execute(
                text(
                    "INSERT INTO investigation_runs (id,incident_id,requested_by,reason,state) "
                    "VALUES (:id,:incident,:actor,'Prior fixture','insufficient_evidence')"
                ),
                {"id": run, "incident": incident, "actor": actor},
            )
        for key, run, signature in (
            (create_key, first, committed_signature(body)),
            (run_key, second, committed_signature(incident_id=incident)),
        ):
            db.execute(
                text(
                    "INSERT INTO admissions (id,actor_id,key,request_hash,incident_id,run_id) "
                    "VALUES (:id,:actor,:key,:hash,:incident,:run)"
                ),
                {
                    "id": uuid4(),
                    "actor": actor,
                    "key": key,
                    "hash": signature,
                    "incident": incident,
                    "run": run,
                },
            )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    headers = sign_in(client)
    replay = create(client, headers, body, create_key)
    assert replay.status_code == 201, replay.text
    assert replay.json() == {
        "incident_id": str(incident),
        "run_id": str(first),
        "state": "insufficient_evidence",
        "queue_reason": None,
        "window": None,
    }
    old_run = client.post(
        f"/api/incidents/{incident}/runs", headers={**headers, "Idempotency-Key": str(run_key)}
    )
    assert old_run.status_code == 201 and old_run.json()["run_id"] == str(second)
    assert create(client, headers, {**body, "question": "New"}, create_key).status_code == 409
    view = client.get(f"/api/incidents/{incident}/runs/{first}").json()
    assert view["window"] is None and view["question_source"] == "not_captured"
    assert view["origin"] == {"kind": "operator", "source_id": None}
    assert view["requested_by"] == str(actor)
    with sessions()() as db:
        assert set(db.scalars(select(Admission.signature_version))) == {1}
        assert db.get(Incident, incident).severity_provenance == "not_provided"


def test_automatic_capacity_defers_then_refuses_without_partial_admission(
    client, capacity, identities
):
    headers = sign_in(client)
    workload = incident_body(client)["workload_id"]
    capacity(jobs=1, deferred=1, filled=0)
    dispatched = automatic(identities, "a", workload)
    assert dispatched.state == "queued" and dispatched.queue_reason is None
    assert count(Job, Job.subject_id == dispatched.run_id) == 1
    before = (count(Incident), count(InvestigationRun), count(Job))
    with pytest.raises(RuntimeError):
        automatic(identities, "d", workload, fail_bind=True)
    assert (count(Incident), count(InvestigationRun), count(Job)) == before
    deferred = automatic(identities, "b", workload)
    assert deferred.queue_reason == "capacity" and deferred.window.source == "automatic_default"
    assert count(Job, Job.subject_id == deferred.run_id) == 0
    with sessions()() as db:
        incident = db.get(Incident, deferred.incident_id)
        assert incident.created_by is None and incident.origin_source_id == "test-alertmanager"
        run = db.get(InvestigationRun, deferred.run_id)
        assert run.requested_by is None and run.question_source == "automatic"
    before = (count(Incident), count(InvestigationRun), count(Job))
    with pytest.raises(DomainError) as refused:
        automatic(identities, "c", workload)
    assert (refused.value.status, refused.value.code) == (503, "capacity_exhausted")
    assert (count(Incident), count(InvestigationRun), count(Job)) == before
    # Accepted identities replay before capacity is consulted, with the current state.
    assert automatic(identities, "b", workload) == deferred
    assert cancel(client, headers, deferred.incident_id, deferred.run_id).status_code == 200
    replay = automatic(identities, "b", workload)
    assert (replay.run_id, replay.state, replay.queue_reason) == (
        deferred.run_id,
        "cancelled",
        None,
    )
    # Manual work never defers: a full dispatch queue stays a conflict.
    full = create(client, headers, incident_body(client))
    assert full.status_code == 409 and full.json()["error"]["code"] == "queue_full"


def test_promotion_is_bounded_oldest_first_and_survives_restart(client, capacity, identities):
    sign_in(client)
    workload = incident_body(client)["workload_id"]
    capacity(jobs=1, deferred=5, filled=1)
    first = automatic(identities, "first", workload)
    second = automatic(identities, "second", workload)
    assert first.queue_reason == second.queue_reason == "capacity"
    assert promote_deferred() == 0
    free_capacity()
    # A fresh process sees only persisted state; promotion needs no new alert or click.
    assert promote_deferred() == 1
    assert run_row(first.run_id).queue_reason is None
    assert run_row(second.run_id).queue_reason == "capacity"
    assert count(Job, Job.subject_id == first.run_id) == 1
    assert promote_deferred() == 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: promote_deferred(), range(4))) == 0
    assert count(Job, Job.state.in_(["queued", "running"])) == 1


def test_promotion_retires_ineligible_requests_without_remapping(client, capacity, identities):
    headers = sign_in(client)
    workload = incident_body(client)["workload_id"]
    capacity(jobs=1, deferred=5, filled=1)
    retired = automatic(identities, "retired", workload)
    resolved = automatic(identities, "resolved", workload)
    with sessions()() as db, db.begin():
        db.get(Incident, resolved.incident_id).state = "resolved"
    with sessions()() as db, db.begin():
        db.get(Workload, UUID(workload)).active = False
    free_capacity()
    assert promote_deferred() == 0
    assert run_row(retired.run_id).outcome_code == "scope_retired"
    assert run_row(resolved.run_id).outcome_code == "incident_resolved"
    assert count(Job, Job.kind == "development_collection_v1") == 0
    assert run_row(retired.run_id).state == "cancelled"
    assert cancel(client, headers, retired.incident_id, retired.run_id).json()["state"] == (
        "cancelled"
    )


def test_cancel_retires_the_job_and_claim_never_attempts_it(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    response = cancel(client, headers, ids["incident_id"], ids["run_id"], reason="Duplicate")
    assert response.status_code == 200 and response.json()["outcome_code"] == "operator_cancelled"
    again = cancel(client, headers, ids["incident_id"], ids["run_id"])
    assert again.status_code == 200
    assert count(IncidentActivity, IncidentActivity.kind == "run_cancelled") == 1
    assert claim_next(HANDLERS) is None
    assert count(JobAttempt) == 0
    with sessions()() as db:
        assert db.scalar(select(Job.state).where(Job.subject_id == UUID(ids["run_id"]))) == (
            "cancelled"
        )
    # A cancelled request frees the one-open-run slot for a fresh explicit request.
    assert rerun(client, headers, ids["incident_id"]).status_code == 201


def test_claimed_collection_is_not_cancelled_and_its_result_stays_history(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    claim = claim_next(HANDLERS)
    refused = cancel(client, headers, ids["incident_id"], ids["run_id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "run_not_cancellable"
    resolved = client.post(
        f"/api/incidents/{ids['incident_id']}/lifecycle",
        json={"action": "resolve", "reason": "Recovered"},
        headers=headers,
    )
    assert resolved.status_code == 200
    assert finish(claim, HANDLERS[claim.kind], result=collect_claim(claim))
    assert run_row(ids["run_id"]).state == "insufficient_evidence"
    detail = client.get(f"/api/incidents/{ids['incident_id']}").json()
    assert detail["state"] == "resolved"
    assert count(Job, Job.state.in_(["queued", "running"])) == 0


def test_resolve_cancels_queued_and_reclaim_after_resolve_never_retries(client):
    headers = sign_in(client)
    queued = create(client, headers, incident_body(client)).json()
    resolved = client.post(
        f"/api/incidents/{queued['incident_id']}/lifecycle",
        json={"action": "resolve", "reason": "Duplicate alert"},
        headers=headers,
    )
    assert resolved.status_code == 200
    run = run_row(queued["run_id"])
    assert (run.state, run.outcome_code) == ("cancelled", "incident_resolved")
    page = client.get(f"/api/incidents/{queued['incident_id']}/activity").json()
    resolution = next(item for item in page["items"] if item["kind"] == "resolve")
    saved = client.get(
        f"/api/incidents/{queued['incident_id']}/handoff/preview",
        params={"resolution_id": resolution["id"]},
    ).json()
    saved_run = next(
        item for item in saved["snapshot"]["runs"]["items"] if item["id"] == queued["run_id"]
    )
    assert (saved_run["state"], saved_run["outcome_code"]) == ("cancelled", "incident_resolved")
    reopened = client.post(
        f"/api/incidents/{queued['incident_id']}/lifecycle",
        json={"action": "reopen", "reason": "Recurring"},
        headers=headers,
    )
    assert reopened.status_code == 200 and run_row(queued["run_id"]).state == "cancelled"
    assert claim_next(HANDLERS) is None

    # Lease expiry after resolve: the expired attempt stays history; no new attempt runs.
    ids = rerun(client, headers, queued["incident_id"]).json()
    claim = claim_next(HANDLERS)
    with sessions()() as db, db.begin():
        db.execute(
            update(Job)
            .where(Job.id == claim.id)
            .values(lease_until=database_now(db) - timedelta(seconds=1))
        )
    assert (
        client.post(
            f"/api/incidents/{queued['incident_id']}/lifecycle",
            json={"action": "resolve", "reason": "Recovered"},
            headers=headers,
        ).status_code
        == 200
    )
    assert run_row(ids["run_id"]).state == "collecting"
    assert claim_next(HANDLERS) is None
    run = run_row(ids["run_id"])
    assert (run.state, run.outcome_code) == ("cancelled", "incident_resolved")
    with sessions()() as db:
        attempts = db.scalars(
            select(JobAttempt.state).join(Job).where(Job.subject_id == run.id)
        ).all()
    assert attempts == ["expired"]
    assert not finish(claim, HANDLERS[claim.kind], error="late worker")


def test_cancel_does_not_wait_for_a_claimer_holding_the_job(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    with sessions()() as holder, holder.begin():
        holder.execute(
            select(Job).where(Job.subject_id == UUID(ids["run_id"])).with_for_update()
        ).all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(
                cancel, client, headers, ids["incident_id"], ids["run_id"]
            ).result(timeout=10)
        assert response.status_code == 200
    # The claimer observes the terminal Run and retires the job without an attempt.
    assert claim_next(HANDLERS) is None
    assert count(JobAttempt) == 0
    assert count(Job, Job.state == "cancelled") == 1


def test_claim_waiting_on_a_cancelling_run_retires_the_job(client):
    from cloudops.features.investigations.service import terminalize

    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    holder = sessions()()
    try:
        holder.begin()
        run = holder.scalar(
            select(InvestigationRun)
            .where(InvestigationRun.id == UUID(ids["run_id"]))
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            claiming = pool.submit(claim_next, HANDLERS)
            deadline = time.monotonic() + 10
            with engine().connect() as monitor:
                while not monitor.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                ).scalar():
                    assert time.monotonic() < deadline, "claimer never waited on the Run lock"
                    time.sleep(0.05)
            terminalize(holder, run, "operator_cancelled")
            holder.commit()
            assert claiming.result(timeout=10) is None
    finally:
        holder.close()
    assert count(JobAttempt) == 0
    assert count(Job, Job.state == "cancelled") == 1
    assert run_row(ids["run_id"]).state == "cancelled"


def test_late_callbacks_cannot_resurrect_a_cancelled_run(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    assert cancel(client, headers, ids["incident_id"], ids["run_id"]).status_code == 200
    handler = HANDLERS["development_collection_v1"]
    result = CollectionResult("uid", ())
    with sessions()() as db, db.begin():
        handler.started(db, UUID(ids["run_id"]))
        handler.failed(db, UUID(ids["run_id"]), "late", terminal=False)
        handler.publish(db, UUID(ids["run_id"]), result)
    run = run_row(ids["run_id"])
    assert (run.state, run.outcome_code) == ("cancelled", "operator_cancelled")
    assert count(Evidence) == 0


def test_drafts_are_invalidated_at_promotion_and_claim(client, capacity, identities):
    sign_in(client)
    workload = incident_body(client)["workload_id"]

    def draft(incident_id):
        with sessions()() as db, db.begin():
            actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
            plan = ActionPlan(
                incident_id=incident_id,
                workload_id=UUID(workload),
                content={"source": "draft_fixture"},
                digest="0" * 64,
                target_epoch="fixture",
                rationale="Draft fixture",
                created_by=actor,
                expires_at=database_now(db) + timedelta(minutes=5),
            )
            db.add(plan)
            db.flush()
            return plan.id

    def state(plan_id):
        with sessions()() as db:
            return db.get(ActionPlan, plan_id).state

    capacity(jobs=1, deferred=5, filled=1)
    deferred = automatic(identities, "draft", workload)
    during_deferral = draft(deferred.incident_id)
    free_capacity()
    assert promote_deferred() == 1
    assert state(during_deferral) == "invalidated"
    after_job = draft(deferred.incident_id)
    assert claim_next(HANDLERS).subject_id == deferred.run_id
    assert state(after_job) == "invalidated"


def test_one_open_run_per_incident_is_enforced_by_the_database(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    with pytest.raises(IntegrityError, match="one_open_run_per_incident"):
        with sessions()() as db, db.begin():
            actor = db.get(InvestigationRun, UUID(ids["run_id"])).requested_by
            db.add(InvestigationRun(incident_id=UUID(ids["incident_id"]), requested_by=actor))
    with pytest.raises(IntegrityError, match="run_origin"):
        with sessions()() as db, db.begin():
            db.add(
                InvestigationRun(
                    incident_id=UUID(ids["incident_id"]), requested_by=None, state="failed"
                )
            )


def test_approval_waits_for_a_pending_investigation(permission_context):
    c = permission_context
    with sessions()() as db, db.begin():
        run = InvestigationRun(
            incident_id=UUID(c.consumer.incident_id),
            requested_by=UUID(c.actors["operator"].session["principal"]["id"]),
            queue_reason="capacity",
        )
        db.add(run)
        db.flush()
        run_id = run.id
    response = decide(c)
    assert response.status_code == 409 and response.json()["error"]["code"] == "run_active"
    with sessions()() as db:
        assert not db.scalar(
            select(PlanReview.consumed).where(PlanReview.plan_id == UUID(c.consumer.plan_id))
        )
        assert (
            db.scalar(select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id)))
            is None
        )
    operator = c.actors["operator"]
    cancelled = cancel(operator.client, operator.headers, c.consumer.incident_id, run_id)
    assert cancelled.status_code == 200
    assert decide(c).status_code == 200


def test_promotion_cancels_behind_an_active_execution_without_touching_its_lock(
    permission_context,
):
    c = permission_context
    assert decide(c).status_code == 200
    with sessions()() as db, db.begin():
        run = InvestigationRun(
            incident_id=UUID(c.consumer.incident_id),
            requested_by=UUID(c.actors["operator"].session["principal"]["id"]),
            queue_reason="capacity",
        )
        db.add(run)
        db.flush()
        run_id = run.id
    with sessions()() as db:
        before = db.scalar(select(Execution).where(Execution.plan_id == UUID(c.consumer.plan_id)))
    promote_deferred()  # The seeded pending fixture may be promoted; this Run may not.
    row = run_row(run_id)
    assert (row.state, row.outcome_code) == ("cancelled", "execution_blocking")
    with sessions()() as db:
        after = db.get(Execution, before.id)
        assert (
            (after.state, after.holds_lock)
            == (before.state, before.holds_lock)
            == (
                "queued",
                True,
            )
        )
        assert db.scalar(select(func.count()).select_from(Job).where(Job.subject_id == run_id)) == 0


def test_worker_cycle_promotes_before_claiming(client, capacity, identities, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    sign_in(client)
    workload = incident_body(client)["workload_id"]
    capacity(jobs=1, deferred=5, filled=1)
    deferred = automatic(identities, "worker", workload)
    free_capacity()
    assert run_once()
    run = run_row(deferred.run_id)
    assert run.state == "insufficient_evidence" and run.queue_reason is None


def test_upgrade_preserves_rows_and_downgrade_refuses_new_semantics(client):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    assert cancel(client, headers, ids["incident_id"], ids["run_id"]).status_code == 200
    with pytest.raises(RuntimeError, match="Downgrade would discard"):
        command.downgrade(config, "0005_workload_observations")
    assert run_row(ids["run_id"]).state == "cancelled"
