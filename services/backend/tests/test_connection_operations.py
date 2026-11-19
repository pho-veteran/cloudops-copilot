from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import sign_in
from sqlalchemy import event, insert, select, update

from cloudops.entrypoints.dispatch import CLASSES, PERMIT_CLASSES
from cloudops.features.access.models import LoginSession, User
from cloudops.features.connections import operations
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.states import COLLECTION_JOB
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, engine
from cloudops.shared.dispatch_permit import transition
from cloudops.shared.jobs import Job

ROUTE = "/api/connections/operations"
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


def read(client):
    response = client.get(ROUTE)
    assert response.status_code == 200, response.text
    return response.json()


def test_job_reasons_and_permit_are_independent(client, db, monkeypatch, dispatch_permit):
    monkeypatch.setattr(operations, "database_now", lambda _: NOW)
    future = NOW + timedelta(minutes=5)
    token = uuid4()
    db.add_all(
        Job(kind=COLLECTION_JOB, subject_id=uuid4(), **fields)
        for fields in (
            {"created_at": NOW - timedelta(minutes=2)},
            {"created_at": NOW - timedelta(minutes=1), "due_at": NOW, "attempts": 1},
            {"due_at": future, "attempts": 1},
            {
                "state": "running",
                "lease_until": NOW,
                "lease_token": token,
                "error": "private worker error",
            },
            {"state": "running", "lease_until": future},
            {"state": "completed", "created_at": NOW - timedelta(days=15)},
            {"state": "held"},
        )
    )
    db.add(Job(kind="unlisted_job", subject_id=uuid4()))
    db.add(Job(kind="recommendation_report_v1", subject_id=uuid4(), state="running"))
    db.commit()
    sign_in(client)
    normal = read(client)
    assert {job["kind"] for job in normal["jobs"]} == set(PERMIT_CLASSES)
    assert normal["oldest_runnable_age_seconds"] == 120
    collection = normal["jobs"][0]
    assert collection["kind"] == COLLECTION_JOB and collection["dispatch"] == "allowed"
    assert collection["counts"] == {
        "queued": 3,
        "running": 2,
        "held": 1,
        "runnable": 2,
        "due_retry": 1,
        "future_retry": 1,
        "expired_lease": 1,
        "earliest_future_retry_at": future.isoformat().replace("+00:00", "Z"),
    }
    report = next(job for job in normal["jobs"] if job["kind"] == "recommendation_report_v1")
    assert report["counts"]["expired_lease"] is None
    transition(
        dispatch_permit,
        "enter_recovery",
        CLASSES,
        reason="Owned operations fixture",
        evidence=["pytest"],
        actor={},
    )
    paused = read(client)["jobs"][0]
    assert paused["dispatch"] == "paused" and paused["dispatch_reason"] == "recovery_mode"
    assert paused["counts"] == collection["counts"]
    assert str(token) not in str(normal) and "private worker error" not in str(normal)


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_operations_requires_a_current_read_session(client, db, role, monkeypatch):
    calls = []
    project = operations.active_jobs_query

    def track_query():
        calls.append(True)
        return project()

    monkeypatch.setattr(operations, "active_jobs_query", track_query)
    assert client.get(ROUTE).status_code == 401
    sign_in(client, role)
    response = client.get(ROUTE)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    db.execute(update(LoginSession).values(expires_at=database_now(db) - timedelta(seconds=1)))
    db.commit()
    response = client.get(ROUTE)
    assert response.status_code == 401 and response.json()["error"]["code"] == "session_expired"
    assert calls == [True]


def seed_run(db, workload_id, actor_id, state, *, run_id=None):
    incident = Incident(
        workload_id=workload_id, created_by=actor_id, title="Operations fixture", description=""
    )
    db.add(incident)
    db.flush()
    run = InvestigationRun(
        id=run_id,
        incident_id=incident.id,
        requested_by=actor_id,
        state=state,
        queue_reason="capacity" if state == "queued" else None,
        created_at=NOW,
    )
    db.add(run)
    db.flush()
    return run.id


def test_active_scan_caps_and_run_sample_order(client, db, monkeypatch):
    monkeypatch.setattr(operations, "JOB_READ_LIMIT", 4)
    monkeypatch.setattr(operations, "RUN_SAMPLE_LIMIT", 2)
    workload = db.scalar(select(Workload.id).limit(1))
    actor = db.scalar(select(User.id).where(User.username == "operator"))
    for number in (3, 1, 2):
        seed_run(db, workload, actor, "queued", run_id=UUID(int=number))
        seed_run(db, workload, actor, "waiting_for_quota", run_id=UUID(int=number + 10))
    # A Run which has a linked collection job is not a capacity blocker.
    linked = seed_run(db, workload, actor, "queued", run_id=UUID(int=0))
    db.add(Job(kind=COLLECTION_JOB, subject_id=linked, state="completed"))
    db.execute(
        insert(Job),
        [
            {"kind": COLLECTION_JOB, "subject_id": uuid4(), "state": "completed"}
            for _ in range(10_000)
        ]
        + [{"kind": COLLECTION_JOB, "subject_id": uuid4()} for _ in range(4)],
    )
    db.commit()
    sign_in(client)
    statements = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement.lower(), parameters))

    event.listen(engine(), "before_cursor_execute", record)
    try:
        snapshot = read(client)
    finally:
        event.remove(engine(), "before_cursor_execute", record)
    assert snapshot["jobs_capped"] is False
    assert snapshot["jobs"][0]["counts"]["queued"] == 4
    assert [row["run_id"] for row in snapshot["capacity_deferred"]] == [
        str(UUID(int=1)),
        str(UUID(int=2)),
    ]
    assert [row["run_id"] for row in snapshot["quota_waiting"]] == [
        str(UUID(int=11)),
        str(UUID(int=12)),
    ]
    assert snapshot["capacity_deferred_has_more"] and snapshot["quota_waiting_has_more"]
    assert all(
        row["incident_title"] == "Operations fixture" for row in snapshot["capacity_deferred"]
    )
    assert all(row["earliest_retry_at"] is None for row in snapshot["quota_waiting"])
    assert all(statement.lstrip().startswith("select") for statement, _ in statements)
    projection = [
        (sql, params) for sql, params in statements if " limit " in sql.replace("\n", " ")
    ]
    assert [parameters["param_1"] for _, parameters in projection] == [5, 3, 3]
    for statement, _ in projection:
        assert not any(
            field in statement
            for field in ("lease_token", ".error", ".payload", ".feedback", "checkpoints")
        )

    db.add(Job(kind=COLLECTION_JOB, subject_id=uuid4()))
    db.commit()
    capped = read(client)
    assert capped["jobs_capped"] and all(job["counts"] is None for job in capped["jobs"])
    assert capped["oldest_runnable_age_seconds"] is None
    assert capped["capacity_deferred"] == snapshot["capacity_deferred"]
