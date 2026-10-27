"""PostgreSQL publication, freshness, retention and queue-isolation evidence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import sign_in
from observation_fixtures import observed
from sqlalchemy import func, select

from cloudops.features.access.models import User
from cloudops.features.incidents.models import Incident
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_models import (
    ObservationAttempt,
    ObservationState,
    WorkloadObservation,
)
from cloudops.features.workloads.observation_service import (
    claim,
    cleanup,
    detail,
    finalize,
    history,
    observe_direct,
    summary,
    sweep,
)
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job


@pytest.fixture
def live_workloads(db):
    with db.begin():
        rows = [
            Workload(
                cluster="lab",
                namespace="demo",
                name="checkout",
                uid="deploy-uid" if index == 0 else f"deploy-{index}",
                source="live_kubernetes",
            )
            for index in range(100)
        ]
        db.add_all(rows)
        db.flush()
        return [row.id for row in rows]


def succeed(workload_id):
    captured = claim(workload_id, "baseline_candidate")
    assert captured
    result = finalize(captured, observed(captured.identity, captured.started_at))
    assert result.outcome == "complete"
    return result.observation


def test_direct_read_uses_new_producer_and_no_open_database_transaction(live_workloads):
    calls = []

    class Reader:
        async def read(self, identity, started_at):
            with sessions()() as db:
                # No other application transaction holds a workload/feature lock.
                assert db.scalar(select(func.count()).select_from(Job)) == 0
                db.execute(__import__("sqlalchemy").text("SET LOCAL lock_timeout = '100ms'"))
                db.scalar(
                    select(Workload)
                    .where(Workload.id == identity.workload_id)
                    .with_for_update(nowait=True)
                )
            calls.append(identity)
            return observed(identity, started_at)

    for _ in range(2):
        result = asyncio.run(observe_direct(live_workloads[0], reader=Reader()))
        assert result.outcome == "complete"
    assert len(calls) == 2
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 2
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        page = history(db, live_workloads[0], 1, None)
        next_page = history(db, live_workloads[0], 1, page.next_cursor)
        assert len(page.items) == len(next_page.items) == 1
        assert page.items[0].observation_id != next_page.items[0].observation_id
        assert next_page.next_cursor is None


def test_global_multi_connection_cap_reserves_a_direct_slot(live_workloads):
    with ThreadPoolExecutor(max_workers=12) as executor:
        claims = list(
            executor.map(lambda identity: claim(identity, "scheduled"), live_workloads[:20])
        )
    assert sum(value is not None for value in claims) == 3
    direct = claim(live_workloads[25], "baseline_confirm")
    assert direct is not None
    assert claim(live_workloads[26], "baseline_candidate") is None
    assert (
        claim(next(value.identity.workload_id for value in claims if value), "baseline_candidate")
        is None
    )
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAttempt)) == 4
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_late_token_cannot_replace_new_attempt_or_clear_its_lease(live_workloads):
    first = claim(live_workloads[0], "baseline_candidate")
    with sessions()() as db, db.begin():
        state = db.get(ObservationState, live_workloads[0])
        state.lease_until = database_now(db) - timedelta(seconds=1)
    second = claim(live_workloads[0], "baseline_confirm")
    assert first.token != second.token
    assert finalize(first, observed(first.identity, first.started_at)).outcome == "superseded"
    with sessions()() as db:
        assert db.get(ObservationState, live_workloads[0]).lease_token == second.token
    assert finalize(second, observed(second.identity, second.started_at)).outcome == "complete"


@pytest.mark.parametrize("change", ["inactive", "epoch", "source"])
def test_retirement_same_uid_readd_and_source_changes_fence_publication(live_workloads, change):
    first = claim(live_workloads[0], "baseline_candidate")
    with sessions()() as db, db.begin():
        row = db.get(Workload, live_workloads[0], with_for_update=True)
        if change == "inactive":
            row.active = False
        elif change == "epoch":
            row.observation_epoch += 1
        else:
            row.source = "development_fixture"
    assert finalize(first, observed(first.identity, first.started_at)).outcome == "superseded"
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 0
        assert detail(db, live_workloads[0]).summary.effective_health == "unknown"


def test_freshness_failure_partial_deadline_recovery_and_historical_data(live_workloads):
    value = succeed(live_workloads[0])
    with sessions()() as db:
        workload = db.get(Workload, live_workloads[0])
        state = db.get(ObservationState, workload.id)
        record = db.get(WorkloadObservation, value.observation_id)
        assert (
            summary(
                workload, state, record, value.observed_at + timedelta(seconds=60)
            ).effective_health
            == "healthy"
        )
        stale = summary(workload, state, record, value.observed_at + timedelta(seconds=60.001))
        assert stale.freshness == "stale" and stale.effective_health == "unknown"
    failed = claim(live_workloads[0], "baseline_confirm")
    finalize(failed, reason="permission_denied")
    with sessions()() as db:
        result = detail(db, live_workloads[0])
        assert result.summary.freshness == "fresh" and result.summary.effective_health == "unknown"
        assert result.summary.last_failure_reason == "permission_denied"
        assert result.historical.observation_id == value.observation_id and result.current is None
    retry = claim(live_workloads[0], "baseline_confirm")
    with sessions()() as db:
        pending = detail(db, live_workloads[0]).summary
        assert pending.refreshing and pending.effective_health == "unknown"
        assert pending.last_failure_reason == "permission_denied"
    value2 = observed(retry.identity, retry.started_at)
    assert finalize(retry, value2).outcome == "complete"
    with sessions()() as db:
        result = detail(db, live_workloads[0])
        assert result.current.observation_id == value2.observation_id
        assert result.summary.last_failure_reason == "permission_denied"
    running = claim(live_workloads[0], "baseline_candidate")
    with sessions()() as db, db.begin():
        state = db.get(ObservationState, live_workloads[0])
        state.deadline_at = database_now(db) - timedelta(seconds=1)
    with sessions()() as db:
        assert detail(db, live_workloads[0]).summary.effective_health == "unknown"
    assert (
        finalize(running, observed(running.identity, running.started_at)).reason
        == "attempt_timeout"
    )


def test_retention_bounds_failure_only_history_and_dead_observer(live_workloads):
    value = succeed(live_workloads[0])
    with sessions()() as db, db.begin():
        now = database_now(db)
        for index in range(110):
            moment = now - timedelta(seconds=index + 1)
            historical = value.model_copy(
                update={
                    "observation_id": uuid4(),
                    "started_at": moment,
                    "observed_at": moment,
                }
            )
            db.add(
                WorkloadObservation(
                    id=historical.observation_id,
                    workload_id=live_workloads[0],
                    epoch=historical.identity.epoch,
                    observed_at=moment,
                    payload=historical.model_dump(mode="json"),
                    comparison={},
                )
            )
            db.add(
                ObservationAttempt(
                    id=uuid4(),
                    workload_id=live_workloads[0],
                    sequence=index,
                    started_at=now - timedelta(seconds=index),
                    outcome="failed",
                    reason="permission_denied",
                )
            )
        db.add(
            ObservationAttempt(
                id=uuid4(),
                workload_id=live_workloads[1],
                sequence=1,
                started_at=now - timedelta(days=8),
                outcome="failed",
                reason="permission_denied",
            )
        )
        db.flush()
        cleanup(db, live_workloads[0], now)
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkloadObservation)
                .where(WorkloadObservation.workload_id == live_workloads[0])
            )
            == 100
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ObservationAttempt)
                .where(ObservationAttempt.workload_id == live_workloads[0])
            )
            == 100
        )
        db.get(WorkloadObservation, value.observation_id).observed_at = now - timedelta(days=8)
    dead = claim(live_workloads[2], "scheduled")
    with sessions()() as db, db.begin():
        db.get(ObservationState, live_workloads[2]).lease_until = database_now(db) - timedelta(
            seconds=1
        )
    sweep()
    with sessions()() as db:
        assert db.get(ObservationAttempt, dead.token).outcome == "expired"
        assert db.get(WorkloadObservation, value.observation_id) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(ObservationAttempt)
                .where(ObservationAttempt.workload_id == live_workloads[1])
            )
            == 0
        )
        assert detail(db, live_workloads[0]).summary.effective_health == "unknown"


def test_observation_errors_preserve_classified_failure_and_no_jobs(live_workloads):
    class Denied:
        async def read(self, identity, started_at):
            raise ObservationError("permission_denied")

    result = asyncio.run(observe_direct(live_workloads[0], reader=Denied()))
    assert result.outcome == "failed" and result.observation is None
    with sessions()() as db:
        state = db.get(ObservationState, live_workloads[0])
        assert state.last_success_at is None and state.next_due_at > state.last_attempt_at
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_history_cursor_does_not_repeat_or_shift_when_a_new_observation_arrives(live_workloads):
    oldest = succeed(live_workloads[0])
    newer = succeed(live_workloads[0])
    with sessions()() as db:
        first = history(db, live_workloads[0], 1, None)
    newest = succeed(live_workloads[0])
    with sessions()() as db:
        following = history(db, live_workloads[0], 1, first.next_cursor)
    assert first.items[0].observation_id == newer.observation_id
    assert following.items[0].observation_id == oldest.observation_id
    assert following.items[0].observation_id != newest.observation_id
    assert following.next_cursor is None


def test_direct_admission_classifies_invalid_scope_even_when_capacity_is_full(
    seeded, live_workloads
):
    with sessions()() as db, db.begin():
        fixture = db.scalar(
            select(Workload.id).where(Workload.source == "development_fixture").limit(1)
        )
        db.get(Workload, live_workloads[0]).active = False
    for identity in live_workloads[1:5]:
        assert claim(identity, "baseline_candidate")

    class ForbiddenReader:
        async def read(self, *_):
            raise AssertionError("Invalid scope must not issue an external read")

    for identity, reason in (
        (uuid4(), "target_changed"),
        (live_workloads[0], "inactive"),
        (fixture, "fixture"),
    ):
        result = asyncio.run(observe_direct(identity, reader=ForbiddenReader()))
        assert result.outcome == "failed" and result.reason == reason
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAttempt)) == 4


def test_incident_filter_is_applied_before_pagination_and_preserves_replaced_identity(
    client, live_workloads
):
    sign_in(client)
    with sessions()() as db, db.begin():
        actor = db.scalar(select(User.id).where(User.username == "operator"))
        for index in range(61):
            db.add(
                Incident(
                    workload_id=live_workloads[index % 2],
                    created_by=actor,
                    title=f"Incident {index}",
                    description="Preserve original input",
                )
            )
        db.get(Workload, live_workloads[0]).active = False
    first = client.get(
        "/api/incidents", params={"workload_id": str(live_workloads[0]), "limit": 25}
    ).json()
    second = client.get(
        "/api/incidents",
        params={"workload_id": str(live_workloads[0]), "limit": 25, "offset": first["next_offset"]},
    ).json()
    assert len(first["items"]) == 25 and len(second["items"]) == 6
    assert all(
        item["workload_id"] == str(live_workloads[0])
        for item in [*first["items"], *second["items"]]
    )
    assert set(item["id"] for item in first["items"]).isdisjoint(
        item["id"] for item in second["items"]
    )
    assert client.get(f"/api/workloads/{live_workloads[0]}").json()["active"] is False
    assert client.get("/api/incidents", params={"workload_id": str(uuid4())}).status_code == 404
    assert client.get("/api/incidents", params={"workload_id": "bad"}).status_code == 422
    assert (
        client.get(f"/api/workloads/{live_workloads[0]}/observations?cursor=bad").status_code == 422
    )


def test_100_scopes_and_blocked_observers_do_not_consume_incident_budget(client, live_workloads):
    headers = sign_in(client)
    for identity in live_workloads[:3]:
        assert claim(identity, "scheduled")
    response = client.post(
        "/api/incidents",
        json={"workload_id": str(live_workloads[50]), "title": "Observer saturated"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(ObservationAttempt)) == 3
