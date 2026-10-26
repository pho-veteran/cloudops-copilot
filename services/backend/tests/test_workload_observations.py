"""PostgreSQL evidence for bounded observation state, leases, freshness and retention."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from cloudops.entrypoints.api import create_app
from cloudops.entrypoints.observer import observe_claim, run_once
from cloudops.features.access.dependencies import COOKIE
from cloudops.features.access.models import LoginSession, User
from cloudops.features.access.service import token_hash
from cloudops.features.incidents.models import Incident
from cloudops.features.workloads.models import (
    Workload,
    WorkloadObservation,
    WorkloadObservationAttempt,
    WorkloadObservationState,
)
from cloudops.features.workloads.observations import (
    claim_due,
    list_observations,
    publish_failure,
    publish_success,
    read_observation,
    request_refresh,
)
from cloudops.features.workloads.schemas import (
    ObservationRefreshView,
    WorkloadObservationPayload,
    WorkloadObservationView,
)
from cloudops.shared.database import database_now, engine, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, claim_next

BACKEND = Path(__file__).resolve().parents[1]


def make_payload(uid="dep-1", status="healthy"):
    return WorkloadObservationPayload(
        deployment_uid=uid,
        generation=1,
        resource_version="1",
        observed_generation=1,
        desired_replicas=1,
        ready_replicas=1,
        available_replicas=1,
        updated_replicas=1,
        total_replicas=1,
        status=status,
        reasons=[],
        coverage={"complete": True, "missing_sources": []},
        pods=[],
        restart_total=0,
        restart_delta=None,
        ready_containers=1,
        total_containers=1,
        template_fields={},
        config_references=[],
    )


def create_workload(uid="dep-1", name="checkout", source="live_kubernetes", active=True):
    with sessions()() as db, db.begin():
        row = Workload(
            cluster="lab",
            namespace="demo",
            name=name,
            uid=uid,
            source=source,
            active=active,
        )
        db.add(row)
        db.flush()
        return row.id


def operator_id():
    with sessions()() as db:
        return db.scalar(select(User.id).where(User.username == "operator"))


def db_now():
    with sessions()() as db:
        return database_now(db)


def force_due(workload_id):
    with sessions()() as db, db.begin():
        now = database_now(db)
        state = db.get(WorkloadObservationState, workload_id)
        if state is None:
            db.add(
                WorkloadObservationState(workload_id=workload_id, due_at=now - timedelta(seconds=1))
            )
        else:
            state.due_at = now - timedelta(seconds=1)


def next_claim(workload_id):
    with sessions()() as db:
        claims = claim_due(db, db_now(), 10)
    claim = next((item for item in claims if item.workload_id == workload_id), None)
    if claim:
        return claim
    force_due(workload_id)
    with sessions()() as db:
        claims = claim_due(db, db_now(), 10)
    return next(claim for claim in claims if claim.workload_id == workload_id)


def test_migration_preserves_existing_rows(seeded):
    workload_id = create_workload()
    config = Config(str(BACKEND / "alembic.ini"))
    command.downgrade(config, "0003_e01_consumers")
    command.upgrade(config, "head")
    with sessions()() as db:
        assert db.get(Workload, workload_id) is not None


def test_refresh_request_is_deduplicated_per_active_workload(seeded):
    workload_id = create_workload()
    actor = operator_id()
    with sessions()() as db:
        first = request_refresh(db, workload_id, actor)
        second = request_refresh(db, workload_id, actor)
    assert first.workload_id == second.workload_id
    with sessions()() as db:
        states = db.scalars(select(WorkloadObservationState)).all()
        assert len(states) == 1
        assert states[0].requested_at is not None
    with sessions()() as db:
        claims = claim_due(db, db_now(), 10)
    assert [claim.workload_id for claim in claims] == [workload_id]
    force_due(workload_id)
    with sessions()() as db:
        assert claim_due(db, db_now(), 10) == []


def test_one_lease_per_workload_and_old_claim_cannot_publish(seeded):
    workload_id = create_workload()
    with sessions()() as db:
        original = claim_due(db, db_now(), 10)[0]
    force_due(workload_id)
    with sessions()() as db:
        assert claim_due(db, db_now(), 10) == []
    with sessions()() as db, db.begin():
        state = db.get(WorkloadObservationState, workload_id)
        state.lease_until = database_now(db) - timedelta(seconds=1)
    replacement = next_claim(workload_id)
    assert replacement.lease_token != original.lease_token
    with sessions()() as db:
        assert publish_success(db, original, make_payload()) is False
    with sessions()() as db:
        assert publish_success(db, replacement, make_payload()) is True


def test_retired_or_uid_changed_workload_rejects_publication(seeded):
    retired = create_workload(name="retired")
    retired_claim = next_claim(retired)
    with sessions()() as db, db.begin():
        db.get(Workload, retired).active = False
    with sessions()() as db:
        assert publish_success(db, retired_claim, make_payload()) is False

    changed = create_workload(uid="dep-2", name="changed")
    changed_claim = next_claim(changed)
    with sessions()() as db, db.begin():
        db.get(Workload, changed).uid = "dep-2-replaced"
    with sessions()() as db:
        assert publish_success(db, changed_claim, make_payload(uid="dep-2")) is False


def test_payload_for_replaced_uid_is_rejected(seeded):
    workload_id = create_workload(uid="dep-1")
    claim = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, claim, make_payload(uid="dep-replaced")) is False
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 0
        state = db.get(WorkloadObservationState, workload_id)
        assert state.last_attempt_state is None


def test_failed_attempt_preserves_labeled_last_success(seeded):
    workload_id = create_workload()
    claim = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, claim, make_payload()) is True
    with sessions()() as db:
        before = read_observation(db, workload_id, database_now(db))
    assert before.availability == "available" and before.observed_at is not None

    failure = next_claim(workload_id)
    with sessions()() as db:
        assert publish_failure(db, failure, "cluster_unavailable") is True
    with sessions()() as db:
        after = read_observation(db, workload_id, database_now(db))
    assert after.observation == before.observation
    assert after.observed_at == before.observed_at
    assert after.last_attempt_state == "failed"
    assert after.last_attempt_reason == "cluster_unavailable"
    with sessions()() as db:
        assert list_observations(db, workload_id, 25, 0).items[0].status == "healthy"


def test_freshness_uses_last_success_and_sixty_second_threshold(seeded):
    workload_id = create_workload()
    claim = next_claim(workload_id)
    with sessions()() as db:
        publish_success(db, claim, make_payload())
    with sessions()() as db, db.begin():
        observation = db.scalar(
            select(WorkloadObservation).where(WorkloadObservation.workload_id == workload_id)
        )
        observation.observed_at = database_now(db) - timedelta(seconds=59)
    with sessions()() as db:
        assert read_observation(db, workload_id, database_now(db)).freshness == "fresh"
    with sessions()() as db, db.begin():
        observation = db.scalar(
            select(WorkloadObservation).where(WorkloadObservation.workload_id == workload_id)
        )
        observation.observed_at = database_now(db) - timedelta(seconds=61)
    with sessions()() as db:
        assert read_observation(db, workload_id, database_now(db)).freshness == "stale"


def test_retention_caps_success_and_attempt_history_at_100_and_seven_days(seeded):
    workload_id = create_workload()
    payload = make_payload().model_dump(mode="json")
    with sessions()() as db, db.begin():
        now = database_now(db)
        expired = WorkloadObservation(
            workload_id=workload_id,
            deployment_uid="dep-1",
            status="healthy",
            payload=payload,
            observed_at=now - timedelta(days=8),
        )
        db.add(expired)
        db.flush()
        expired_id = expired.id
        for index in range(105):
            db.add(
                WorkloadObservation(
                    workload_id=workload_id,
                    deployment_uid="dep-1",
                    status="healthy",
                    payload=payload,
                    observed_at=now - timedelta(seconds=300 - index),
                )
            )
            db.add(
                WorkloadObservationAttempt(
                    workload_id=workload_id,
                    state="failed",
                    reason="cluster_unavailable",
                    attempted_at=now - timedelta(seconds=300 - index),
                )
            )
    claim = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, claim, make_payload()) is True
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkloadObservation)
                .where(WorkloadObservation.workload_id == workload_id)
            )
            == 100
        )
        assert db.get(WorkloadObservation, expired_id) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkloadObservationAttempt)
                .where(WorkloadObservationAttempt.workload_id == workload_id)
            )
            == 100
        )


def test_restart_does_not_relabel_observation_fresh(seeded):
    workload_id = create_workload()
    claim = next_claim(workload_id)
    with sessions()() as db:
        publish_success(db, claim, make_payload())
    with sessions()() as db, db.begin():
        observation = db.scalar(
            select(WorkloadObservation).where(WorkloadObservation.workload_id == workload_id)
        )
        observation.observed_at = database_now(db) - timedelta(seconds=61)
    engine().dispose()
    with sessions()() as db:
        assert read_observation(db, workload_id, database_now(db)).freshness == "stale"


def login(client, username="operator"):
    response = client.post(
        "/api/login",
        json={"username": username, "password": "local-test-password"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": "http://localhost:5173",
        "X-CSRF-Token": response.json()["csrf_token"],
    }


class FakeReader:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def observe(self, workload, previous_payload=None):
        if self.error is not None:
            raise self.error
        return self.payload


class ExplodingReader:
    def observe(self, workload, previous_payload=None):
        raise AssertionError("Observer must not read fixture workloads")


def publish_one(workload_id, uid="dep-1"):
    claim = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, claim, make_payload(uid=uid)) is True


def test_viewer_and_operator_can_read_observation(seeded):
    workload_id = create_workload()
    publish_one(workload_id)
    with TestClient(create_app()) as client:
        for role in ("viewer", "operator"):
            login(client, role)
            response = client.get(f"/api/workloads/{workload_id}/observation")
            assert response.status_code == 200, response.text
            view = WorkloadObservationView.model_validate(response.json())
            assert view.availability == "available"
            assert view.observation.status == "healthy"
            history = client.get(f"/api/workloads/{workload_id}/observations")
            assert history.status_code == 200, history.text
            assert len(history.json()["items"]) == 1


def test_anonymous_expired_and_revoked_sessions_cannot_read(seeded):
    workload_id = create_workload()
    with TestClient(create_app()) as client:
        assert client.get(f"/api/workloads/{workload_id}/observation").status_code == 401
        login(client, "viewer")
        assert client.get(f"/api/workloads/{workload_id}/observation").status_code == 200
        cookie = client.cookies[COOKIE]
        with sessions()() as db, db.begin():
            db.execute(
                update(LoginSession)
                .where(LoginSession.token_hash == token_hash(cookie))
                .values(expires_at=database_now(db) - timedelta(seconds=1))
            )
        assert client.get(f"/api/workloads/{workload_id}/observation").status_code == 401
        headers = login(client, "viewer")
        assert client.post("/api/logout", headers=headers).status_code == 204
        assert client.get(f"/api/workloads/{workload_id}/observation").status_code == 401


def test_only_operator_can_request_refresh(seeded):
    workload_id = create_workload()
    with TestClient(create_app()) as client:
        viewer = login(client, "viewer")
        response = client.post(f"/api/workloads/{workload_id}/observation-refresh", headers=viewer)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"
        operator = login(client, "operator")
        response = client.post(
            f"/api/workloads/{workload_id}/observation-refresh", headers=operator
        )
        assert response.status_code == 202, response.text
        assert ObservationRefreshView.model_validate(response.json()).status == "pending"


def test_unknown_workload_cannot_be_observed_or_refreshed(seeded):
    missing = uuid4()
    with TestClient(create_app()) as client:
        operator = login(client, "operator")
        response = client.get(f"/api/workloads/{missing}/observation")
        assert response.status_code == 404, response.text
        response = client.post(f"/api/workloads/{missing}/observation-refresh", headers=operator)
        assert response.status_code == 404, response.text


def test_retired_workload_history_remains_readable_but_refresh_is_rejected(seeded):
    workload_id = create_workload()
    publish_one(workload_id)
    with sessions()() as db, db.begin():
        db.get(Workload, workload_id).active = False
    with TestClient(create_app()) as client:
        operator = login(client, "operator")
        response = client.get(f"/api/workloads/{workload_id}/observation")
        assert response.status_code == 200, response.text
        view = WorkloadObservationView.model_validate(response.json())
        assert view.workload_active is False
        assert view.availability == "available"
        response = client.post(
            f"/api/workloads/{workload_id}/observation-refresh", headers=operator
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "workload_unavailable"


def test_refresh_request_returns_pending_without_calling_kubernetes_in_api(seeded, monkeypatch):
    workload_id = create_workload()

    def explode(*args, **kwargs):
        raise AssertionError("The API must not read Kubernetes during refresh")

    monkeypatch.setattr("cloudops.entrypoints.observer.Kubernetes", explode)
    with TestClient(create_app()) as client:
        operator = login(client, "operator")
        response = client.post(
            f"/api/workloads/{workload_id}/observation-refresh", headers=operator
        )
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "pending"


def test_observer_publishes_success_or_sanitized_failure(seeded):
    workload_id = create_workload()
    claim = next_claim(workload_id)
    assert observe_claim(claim, FakeReader(payload=make_payload())) is True
    with sessions()() as db:
        assert read_observation(db, workload_id, database_now(db)).availability == "available"

    failure = next_claim(workload_id)
    error = DomainError(503, "cluster_unavailable", "provider-body-sentinel")
    assert observe_claim(failure, FakeReader(error=error)) is True
    with sessions()() as db:
        view = read_observation(db, workload_id, database_now(db))
        attempt = db.scalar(
            select(WorkloadObservationAttempt).where(
                WorkloadObservationAttempt.workload_id == workload_id
            )
        )
    assert view.last_attempt_state == "failed"
    assert view.last_attempt_reason == "cluster_unavailable"
    assert "provider-body-sentinel" not in repr(attempt.reason)


def test_observer_never_claims_fixture_health(seeded):
    create_workload(name="fixture", source="development_fixture")
    assert run_once(kube=ExplodingReader()) == 0
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 0


def test_observer_does_not_claim_stale_lease(seeded):
    workload_id = create_workload()
    original = next_claim(workload_id)
    with sessions()() as db, db.begin():
        now = database_now(db)
        state = db.get(WorkloadObservationState, workload_id)
        state.lease_until = now - timedelta(seconds=1)
        state.due_at = now - timedelta(seconds=1)
    replacement = next_claim(workload_id)
    assert observe_claim(original, FakeReader(payload=make_payload())) is False
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 0
    assert observe_claim(replacement, FakeReader(payload=make_payload())) is True


def add_incident(workload_id, title="Related incident"):
    with sessions()() as db, db.begin():
        actor = db.scalar(select(User.id).where(User.username == "operator"))
        row = Incident(workload_id=workload_id, title=title, description="", created_by=actor)
        db.add(row)
        db.flush()
        return row.id


def related_page(client, workload_id, limit=25, offset=0):
    response = client.get(
        "/api/incidents",
        params={"workload_uuid": str(workload_id), "limit": limit, "offset": offset},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_related_incidents_filter_by_uuid_before_pagination(seeded):
    first = create_workload(name="first")
    second = create_workload(name="second")
    for index in range(3):
        add_incident(first, f"First {index}")
    add_incident(second, "Second")
    with TestClient(create_app()) as client:
        login(client, "viewer")
        page = related_page(client, first, limit=2)
        assert len(page["items"]) == 2
        assert {item["workload_id"] for item in page["items"]} == {str(first)}
        assert page["next_offset"] == 2
        tail = related_page(client, first, limit=2, offset=2)
        assert len(tail["items"]) == 1
        assert tail["next_offset"] is None


def test_replacement_uid_incidents_are_excluded(seeded):
    old = create_workload(uid="uid-old", name="same-name")
    new = create_workload(uid="uid-new", name="same-name")
    old_incident = add_incident(old, "Old identity")
    new_incident = add_incident(new, "New identity")
    with TestClient(create_app()) as client:
        login(client, "viewer")
        page = related_page(client, new)
    ids = {item["id"] for item in page["items"]}
    assert str(new_incident) in ids
    assert str(old_incident) not in ids


def test_historical_incidents_for_same_uuid_remain_visible(seeded):
    workload_id = create_workload()
    first = add_incident(workload_id, "Historical one")
    second = add_incident(workload_id, "Historical two")
    with TestClient(create_app()) as client:
        login(client, "viewer")
        page = related_page(client, workload_id)
    assert {item["id"] for item in page["items"]} == {str(first), str(second)}


def test_empty_related_incident_page_has_null_next_offset(seeded):
    workload_id = create_workload()
    with TestClient(create_app()) as client:
        login(client, "viewer")
        page = related_page(client, workload_id)
    assert page == {"items": [], "next_offset": None}


def test_observer_mounts_only_reader_credentials():
    root = Path(__file__).resolve().parents[3]
    text = (root / "compose.integration.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("  observer:")
    block = []
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        block.append(line)
    observer = "\n".join(block)
    assert "/run/cloudops-reader" in observer
    for forbidden in (
        "cloudops-writer",
        "cloudops-dispatch",
        "cloudops-krr",
        "EXECUTOR_DISPATCH_KEY",
        "KRR_RUNNER_KEY",
    ):
        assert forbidden not in observer


class UidReader:
    """Reads the exact claimed identity so each publication stays UID-scoped."""

    def observe(self, workload, previous_payload=None):
        return make_payload(uid=workload.uid)


class BlockingReader:
    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered = entered
        self.release = release

    def observe(self, workload, previous_payload=None):
        self.entered.set()
        assert self.release.wait(timeout=10), "Test did not release the observer"
        return make_payload(uid=workload.uid)


class ProbeHandler:
    """Minimal shared-Job handler proving admission and execution stay independent."""

    def started(self, db, subject_id):
        pass

    def failed(self, db, subject_id, reason, terminal):
        pass

    def publish(self, db, subject_id, result):
        pass


def test_100_workload_scope_exhaustion_reports_stale_coverage(seeded, monkeypatch):
    with sessions()() as db, db.begin():
        now = database_now(db)
        identities = []
        for index in range(100):
            row = Workload(
                cluster="lab",
                namespace="demo",
                name=f"scope-{index}",
                uid=f"scope-{index}",
                source="live_kubernetes",
                active=True,
            )
            db.add(row)
            db.flush()
            identities.append((row.id, row.uid))
        for workload_id, uid in identities:
            db.add(
                WorkloadObservation(
                    workload_id=workload_id,
                    deployment_uid=uid,
                    status="healthy",
                    payload=make_payload(uid=uid).model_dump(mode="json"),
                    observed_at=now - timedelta(seconds=61),
                )
            )
            db.add(
                WorkloadObservationState(
                    workload_id=workload_id,
                    due_at=now - timedelta(seconds=1),
                    last_success_at=now - timedelta(seconds=61),
                    last_attempt_at=now - timedelta(seconds=61),
                    last_attempt_state="succeeded",
                )
            )

    monkeypatch.setattr(
        "cloudops.entrypoints.observer.settings",
        lambda: SimpleNamespace(observation_concurrency=4),
    )
    assert run_once(kube=UidReader()) == 4

    with sessions()() as db:
        read_at = database_now(db)
        views = [read_observation(db, workload_id, read_at) for workload_id, _ in identities]
    assert sum(view.freshness == "fresh" for view in views) == 4
    assert sum(view.freshness == "stale" for view in views) == 96
    assert all(view.availability == "available" for view in views)
    assert all(view.observation.status == "healthy" for view in views)
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkloadObservation)
                .where(WorkloadObservation.observed_at >= read_at - timedelta(seconds=60))
            )
            == 4
        )


def test_blocked_observer_does_not_consume_job_admission_or_execution(seeded):
    workload_id = create_workload()
    claim = next_claim(workload_id)
    entered = threading.Event()
    release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(observe_claim, claim, BlockingReader(entered, release))
        try:
            assert entered.wait(timeout=10), "Observer never started its cluster read"
            with sessions()() as db, db.begin():
                db.add(Job(kind="observation_isolation_probe", subject_id=uuid4()))
            admitted = claim_next({"observation_isolation_probe": ProbeHandler()})
            assert admitted is not None
            assert not future.done(), "Observer must still be blocked on the read"
        finally:
            release.set()
        assert future.result(timeout=10) is True
    with sessions()() as db:
        assert read_observation(db, workload_id, database_now(db)).availability == "available"


def test_refresh_timeout_preserves_last_success_as_historical(seeded):
    workload_id = create_workload()
    success = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, success, make_payload()) is True
    timeout = next_claim(workload_id)
    with sessions()() as db:
        assert publish_failure(db, timeout, "timeout") is True
    with sessions()() as db:
        view = read_observation(db, workload_id, database_now(db))
        history = list_observations(db, workload_id, 25, 0)
    assert view.observation is not None and view.observation.status == "healthy"
    assert view.observed_at is not None and view.last_attempt_at is not None
    assert view.last_attempt_at > view.observed_at
    assert view.last_attempt_state == "failed"
    assert view.last_attempt_reason == "timeout"
    assert view.pending_refresh is False
    assert [item.status for item in history.items] == ["healthy"]


def test_restart_preserves_stale_status_and_sanitized_reason(seeded):
    workload_id = create_workload()
    success = next_claim(workload_id)
    with sessions()() as db:
        assert publish_success(db, success, make_payload()) is True
    failure = next_claim(workload_id)
    with sessions()() as db:
        assert publish_failure(db, failure, "provider-body-sentinel") is True
    with sessions()() as db, db.begin():
        observation = db.scalar(
            select(WorkloadObservation).where(WorkloadObservation.workload_id == workload_id)
        )
        observation.observed_at = database_now(db) - timedelta(seconds=61)
        attempt = db.scalar(
            select(WorkloadObservationAttempt).where(
                WorkloadObservationAttempt.workload_id == workload_id
            )
        )
        assert attempt.reason == "observation_failed"
        assert "provider-body-sentinel" not in repr(attempt.reason)
    engine().dispose()
    with sessions()() as db:
        view = read_observation(db, workload_id, database_now(db))
    assert view.availability == "available"
    assert view.observation is not None and view.observation.status == "healthy"
    assert view.freshness == "stale"
    assert view.last_attempt_state == "failed"
    assert view.last_attempt_reason == "observation_failed"
