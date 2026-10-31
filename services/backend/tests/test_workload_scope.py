"""PostgreSQL scope/identity receipts; Kubernetes responses here are explicit fixtures."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from conftest import sign_in
from sqlalchemy import func, select

from cloudops.features.access.models import LoginSession, User
from cloudops.features.incidents.models import Incident
from cloudops.features.workloads import reconcile
from cloudops.features.workloads.models import (
    Baseline,
    BaselineCandidate,
    ScopeState,
    ScopeTarget,
    Workload,
)
from cloudops.features.workloads.scope_config import Lookup
from cloudops.features.workloads.service import sync_config
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job


@pytest.fixture
def scope_lab(tmp_path, monkeypatch):
    path = tmp_path / "scope.json"
    state = {"checkout": Lookup("present", "uid-v1"), "catalog": Lookup("present", "uid-catalog")}
    policy = {
        "schema_version": 2,
        "cluster": {
            "key": "lab",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": "a" * 64,
        },
        "deployments": [{"namespace": "demo", "name": "checkout"}],
    }

    def write():
        path.write_text(json.dumps(policy))

    class Reader:
        def __init__(self, *args):
            pass

        def lookup(self, target, deadline):
            return state[target.name]

    monkeypatch.setattr(reconcile, "ScopeReader", Reader)
    monkeypatch.setenv("WORKLOADS_FILE", str(path))
    settings.cache_clear()
    write()
    yield policy, state, write
    settings.cache_clear()


def sync():
    with sessions()() as db:
        return sync_config(db)


def rows():
    with sessions()() as db:
        return db.scalars(select(Workload).order_by(Workload.uid)).all()


@pytest.mark.parametrize(
    "lookup", [Lookup("unavailable", reason="permission_denied"), Lookup("present", "uid-v1")]
)
def test_v2_never_binds_a_colliding_fixture_identity(scope_lab, lookup):
    _, state, _ = scope_lab
    with sessions()() as db, db.begin():
        fixture = Workload(
            cluster="lab",
            namespace="demo",
            name="checkout",
            uid="fixture-only",
            source="development_fixture",
        )
        db.add(fixture)
        db.flush()
        fixture_id = fixture.id
    state["checkout"] = lookup
    sync()
    with sessions()() as db:
        fixture = db.get(Workload, fixture_id)
        target = db.scalar(select(ScopeTarget))
        assert not fixture.active and fixture.retirement_reason == "removed"
        assert fixture.source == "development_fixture"
        if lookup.state == "unavailable":
            assert target.workload_id is None
        else:
            assert target.workload_id != fixture_id
            assert db.get(Workload, target.workload_id).source == "live_kubernetes"


def test_v2_rejects_reusing_exact_fixture_uid(scope_lab):
    _, state, _ = scope_lab
    with sessions()() as db, db.begin():
        fixture = Workload(
            cluster="lab",
            namespace="demo",
            name="checkout",
            uid="uid-v1",
            source="development_fixture",
        )
        db.add(fixture)
        db.flush()
        fixture_id = fixture.id
    with pytest.raises(DomainError, match="fixture identity"):
        sync()
    with sessions()() as db:
        assert db.get(Workload, fixture_id).active
        assert db.get(Workload, fixture_id).source == "development_fixture"
        assert db.scalar(select(func.count()).select_from(ScopeTarget)) == 0


@pytest.mark.parametrize("change", ["deleted", "replaced", "removed"])
def test_cli_retirement_count_includes_all_live_retirement_causes(scope_lab, change):
    policy, state, write = scope_lab
    sync()
    if change == "removed":
        policy["deployments"] = []
        write()
    else:
        state["checkout"] = (
            Lookup("missing", reason="not_found")
            if change == "deleted"
            else Lookup("present", "new-uid")
        )
    assert sync().removed == 1
    assert sync().removed == 0


def test_first_missing_unavailable_and_unlisted_are_not_fake_workloads(scope_lab):
    policy, state, write = scope_lab
    policy["deployments"] += [
        {"namespace": "demo", "name": "catalog"},
        {"namespace": "demo", "name": "missing"},
        {"namespace": "demo", "name": "denied"},
    ]
    state.update(
        missing=Lookup("missing", reason="not_found"),
        denied=Lookup("unavailable", reason="permission_denied"),
        unlisted=Lookup("present", "must-not-admit"),
    )
    write()
    result = sync()
    assert (result.current, result.missing, result.unavailable) == (2, 1, 1)
    assert {row.uid for row in rows()} == {"uid-v1", "uid-catalog"}
    with sessions()() as db:
        targets = {t.name: t for t in db.scalars(select(ScopeTarget))}
        assert targets["missing"].workload_id is targets["denied"].workload_id is None
        assert targets["denied"].last_success_at is None
        assert "unlisted" not in targets


def test_idempotence_and_replacement_preserve_confirmed_history(client, scope_lab):
    sync()
    old = next(row for row in rows() if row.active)
    with sessions()() as db, db.begin():
        actor = db.scalar(select(User).where(User.username == "operator"))
        candidate = BaselineCandidate(
            workload_id=old.id, digest="d" * 64, snapshot={"immutable": "original-bytes"}
        )
        db.add(candidate)
        db.flush()
        baseline = Baseline(
            workload_id=old.id,
            candidate_id=candidate.id,
            digest=candidate.digest,
            snapshot=candidate.snapshot,
            confirmed_by=actor.id,
        )
        incident = Incident(
            workload_id=old.id,
            title="Retain",
            description="Original authored input",
            created_by=actor.id,
        )
        db.add_all([baseline, incident])
        db.flush()
        saved = (
            baseline.id,
            baseline.digest,
            baseline.snapshot,
            baseline.confirmed_at,
            incident.id,
        )
    sync()
    assert next(row for row in rows() if row.active).id == old.id
    scope_lab[1]["checkout"] = Lookup("present", "uid-v2")
    sync()
    current = next(row for row in rows() if row.active)
    assert current.id != old.id and current.uid == "uid-v2"
    with sessions()() as db:
        previous = db.get(Workload, old.id)
        assert not previous.active and previous.retirement_reason == "replaced"
        record = db.get(Baseline, saved[0])
        assert (record.digest, record.snapshot, record.confirmed_at) == saved[1:4]
        assert db.get(Incident, saved[4]).workload_id == old.id
        assert (
            db.scalar(
                select(func.count()).select_from(Baseline).where(Baseline.workload_id == current.id)
            )
            == 0
        )
    headers = sign_in(client)
    assert client.get(f"/api/workloads/{old.id}").json()["retirement_reason"] == "replaced"
    body = {
        "workload_id": str(old.id),
        "title": "Denied old identity",
        "description": "History stays readable",
    }
    with sessions()() as db:
        before = db.scalar(select(func.count()).select_from(Job))
    assert (
        client.post(
            "/api/incidents", json=body, headers={**headers, "Idempotency-Key": str(uuid4())}
        ).status_code
        == 422
    )
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before
    assert client.get(f"/api/incidents/{saved[4]}").status_code == 200


@pytest.mark.parametrize("reason", ["permission_denied", "cluster_unavailable", "budget_exhausted"])
def test_failed_read_preserves_prior_identity_admission_and_last_success(scope_lab, reason):
    sync()
    original = rows()[0]
    with sessions()() as db:
        timestamp = db.scalar(select(ScopeTarget.last_success_at))
    scope_lab[1]["checkout"] = Lookup("unavailable", reason=reason)
    assert sync().unavailable == 1
    retained = rows()[0]
    assert retained.id == original.id and retained.active
    with sessions()() as db:
        target = db.scalar(select(ScopeTarget))
        assert target.state == "unavailable" and target.reason == reason
        assert target.last_success_at == timestamp and target.last_attempt_at >= timestamp


@pytest.mark.parametrize("removed", [False, True])
def test_404_or_explicit_removal_closes_admission_and_retains_history(scope_lab, removed):
    policy, state, write = scope_lab
    sync()
    original = rows()[0]
    if removed:
        policy["deployments"] = []
        write()
    else:
        state["checkout"] = Lookup("missing", reason="not_found")
    sync()
    assert rows()[0].id == original.id and not rows()[0].active
    assert rows()[0].retirement_reason == ("removed" if removed else "deleted")
    policy["deployments"] = [{"namespace": "demo", "name": "checkout"}]
    state["checkout"] = Lookup("present", original.uid)
    write()
    sync()
    assert len(rows()) == 1 and rows()[0].id == original.id and rows()[0].active


def test_legacy_to_v2_same_key_uid_keeps_uuid_and_rejects_key_rename(scope_lab):
    policy, _, write = scope_lab
    with sessions()() as db, db.begin():
        old = Workload(
            cluster="lab", namespace="demo", name="checkout", uid="uid-v1", source="live_kubernetes"
        )
        db.add(old)
        db.flush()
        identifier = old.id
    policy["cluster"]["key"] = "renamed"
    write()
    with pytest.raises(DomainError):
        sync()
    assert rows()[0].active and rows()[0].id == identifier
    policy["cluster"]["key"] = "lab"
    write()
    sync()
    assert len(rows()) == 1 and rows()[0].id == identifier


def test_v2_cannot_silently_downgrade_or_rotate_binding(scope_lab):
    policy, _, write = scope_lab
    sync()
    original = rows()[0]
    policy["cluster"]["expected_fingerprint"] = "b" * 64
    write()
    with pytest.raises(DomainError):
        sync()
    with sessions()() as db:
        sync_config(db, "a" * 64)
    assert rows()[0].id == original.id
    settings().workloads_file.write_text("[]")
    with pytest.raises(DomainError):
        sync()
    assert rows()[0].active


def test_rotation_refuses_changed_scope_or_uid(scope_lab):
    policy, state, write = scope_lab
    sync()
    policy["cluster"]["expected_fingerprint"] = "b" * 64
    state["checkout"] = Lookup("present", "another-cluster-uid")
    write()
    with sessions()() as db, pytest.raises(DomainError):
        sync_config(db, "a" * 64)
    assert rows()[0].active and rows()[0].uid == "uid-v1"


def test_empty_scope_cannot_establish_cluster_continuity_for_rotation(scope_lab):
    policy, _, write = scope_lab
    policy["deployments"] = []
    write()
    sync()
    policy["cluster"]["expected_fingerprint"] = "b" * 64
    write()
    with sessions()() as db, pytest.raises(DomainError):
        sync_config(db, "a" * 64)


@pytest.mark.parametrize("newer_fails", [False, True])
def test_superseded_slow_sync_cannot_publish_even_if_newer_fails(
    scope_lab, monkeypatch, newer_fails
):
    sync()
    started, release = Event(), Event()
    original_reader = reconcile.ScopeReader

    class SlowReader(original_reader):
        def lookup(self, target, deadline):
            started.set()
            assert release.wait(10)
            return Lookup("present", "old-delayed-uid")

    monkeypatch.setattr(reconcile, "ScopeReader", SlowReader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(sync)
        assert started.wait(10)
        monkeypatch.setattr(reconcile, "ScopeReader", original_reader)
        scope_lab[1]["checkout"] = Lookup("present", "latest-uid")
        if newer_fails:
            # Reserve a real newer attempt and then fail its external read.
            class FailedReader(original_reader):
                def lookup(self, target, deadline):
                    raise RuntimeError("controlled process failure")

            monkeypatch.setattr(reconcile, "ScopeReader", FailedReader)
            with pytest.raises(RuntimeError):
                sync()
        else:
            sync()
        release.set()
        with pytest.raises(DomainError):
            old.result(timeout=10)
    assert {r.uid for r in rows() if r.active} == ({"uid-v1"} if newer_fails else {"latest-uid"})
    assert "old-delayed-uid" not in {r.uid for r in rows()}


def test_config_change_during_read_discards_staged_results(scope_lab, monkeypatch):
    sync()

    class ChangedReader:
        def __init__(self, *args):
            pass

        def lookup(self, target, deadline):
            scope_lab[0]["deployments"] = []
            scope_lab[2]()
            return Lookup("present", "not-committed")

    monkeypatch.setattr(reconcile, "ScopeReader", ChangedReader)
    with pytest.raises(DomainError):
        sync()
    assert {r.uid for r in rows()} == {"uid-v1"} and rows()[0].active


def test_lookup_holds_no_transaction_or_workload_lock(scope_lab, monkeypatch):
    sync()

    class InspectionReader:
        def __init__(self, *args):
            pass

        def lookup(self, target, deadline):
            with sessions()() as independent, independent.begin():
                assert independent.scalar(select(Workload).with_for_update(nowait=True)) is not None
                assert (
                    independent.scalar(select(ScopeState).with_for_update(nowait=True)) is not None
                )
            return Lookup("present", "uid-v1")

    monkeypatch.setattr(reconcile, "ScopeReader", InspectionReader)
    sync()


def test_scope_api_exposes_unresolved_and_removed_with_real_role_guards(client, scope_lab):
    scope_lab[1]["checkout"] = Lookup("unavailable", reason="permission_denied")
    sync()
    assert client.get("/api/workloads/scope").status_code == 401
    sign_in(client, "viewer")
    record = client.get("/api/workloads/scope").json()["items"][0]
    assert record["workload_id"] is record["uid"] is None
    assert record["state"] == "unavailable" and record["reason"] == "permission_denied"
    scope_lab[0]["deployments"] = []
    scope_lab[2]()
    sync()
    assert client.get("/api/workloads/scope").json()["items"] == []
    assert client.get("/api/workloads/scope?removed=true").json()["items"][0]["state"] == "removed"
    assert client.post("/api/workloads/scope").status_code == 405
    with sessions()() as db, db.begin():
        for session in db.scalars(select(LoginSession)):
            session.expires_at = database_now(db) - timedelta(seconds=1)
    assert client.get("/api/workloads/scope").status_code == 401


def test_scope_pagination_is_before_projection(scope_lab):
    policy, state, write = scope_lab
    policy["deployments"] = [{"namespace": "demo", "name": f"item-{i:02}"} for i in range(27)]
    state.update({f"item-{i:02}": Lookup("missing", reason="not_found") for i in range(27)})
    write()
    sync()
    from cloudops.features.workloads.service import scope_inventory

    with sessions()() as db:
        first = scope_inventory(db, 25, 0, False)
        second = scope_inventory(db, 25, first.next_offset, False)
        assert len(first.items) == 25 and len(second.items) == 2 and second.next_offset is None
        assert len({t.id for t in first.items + second.items}) == 27


def test_publication_failure_rolls_back_retirement_and_new_identity(scope_lab, monkeypatch):
    sync()
    original = rows()[0]
    scope_lab[1]["checkout"] = Lookup("present", "must-roll-back")
    publish = reconcile.publish_live

    def fail_after_staging(*args):
        publish(*args)
        raise RuntimeError("Controlled failure before publication commit")

    monkeypatch.setattr(reconcile, "publish_live", fail_after_staging)
    with pytest.raises(RuntimeError):
        sync()
    assert len(rows()) == 1 and rows()[0].id == original.id and rows()[0].active
    with sessions()() as db:
        assert db.scalar(select(ScopeTarget)).state == "current"
        assert db.scalar(select(ScopeState)).applied_sequence == 1


def test_invalid_duplicate_policy_does_not_change_identity_or_start_reader(scope_lab, monkeypatch):
    sync()
    identifier = rows()[0].id
    scope_lab[0]["deployments"] *= 2
    scope_lab[2]()

    def unexpected(*args):
        pytest.fail("Invalid policy must not construct a reader")

    monkeypatch.setattr(reconcile, "ScopeReader", unexpected)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        sync()
    assert rows()[0].active and rows()[0].id == identifier


def test_retirement_serializes_with_real_incident_admission(client, scope_lab, monkeypatch):
    from cloudops.features.incidents import service

    sync()
    workload = next(row for row in rows() if row.active)
    headers = sign_in(client)
    entered, release, read_completed = Event(), Event(), Event()
    real_admit = service.admit_run

    def delayed_admit(*args):
        entered.set()
        assert release.wait(10)
        return real_admit(*args)

    monkeypatch.setattr(service, "admit_run", delayed_admit)
    reader = reconcile.ScopeReader

    class RetirementReader(reader):
        def lookup(self, target, deadline):
            read_completed.set()
            return Lookup("missing", reason="not_found")

    body = {
        "workload_id": str(workload.id),
        "title": "Admission before retirement",
        "description": "Serialized boundary",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(
            client.post,
            "/api/incidents",
            json=body,
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert entered.wait(10)
        monkeypatch.setattr(reconcile, "ScopeReader", RetirementReader)
        retirement = pool.submit(sync)
        try:
            assert read_completed.wait(10)
        finally:
            release.set()
        assert admission.result(timeout=10).status_code == 201
        assert retirement.result(timeout=10).missing == 1
    assert (
        client.post(
            "/api/incidents", json=body, headers={**headers, "Idempotency-Key": str(uuid4())}
        ).status_code
        == 422
    )
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Incident)
                .where(Incident.workload_id == workload.id)
            )
            == 1
        )
