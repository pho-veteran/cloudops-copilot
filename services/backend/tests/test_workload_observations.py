"""PostgreSQL evidence for bounded observation state, leases, freshness and retention."""

from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from cloudops.features.access.models import User
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
from cloudops.features.workloads.schemas import WorkloadObservationPayload
from cloudops.shared.database import database_now, engine, sessions

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
