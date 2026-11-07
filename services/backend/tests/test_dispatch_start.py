"""Real PostgreSQL call-start fencing; transport observations are explicit test doubles."""

import threading
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import checkpoint_adapters as adapters
import pytest
from conftest import sign_in
from sqlalchemy import select
from test_access_admission import create, incident_body
from test_collection_checkpoints import bound, context_for  # noqa: F401
from test_dispatch_gate import change
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints import worker
from cloudops.entrypoints.dispatch import GATE, PERMIT_CLASSES
from cloudops.features.investigations import collector
from cloudops.features.recommendations import worker as reports
from cloudops.features.recovery import service as recovery
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.shared import jobs
from cloudops.shared.database import database_now, sessions
from cloudops.shared.file_lock import LockUnavailable, file_lock

permission_context = _permission_context


class Handler:
    def started(self, db, subject_id):
        pass


def claim_for(kind, subject_id=None):
    with sessions()() as db, db.begin():
        db.add(jobs.Job(kind=kind, subject_id=subject_id or uuid4()))
    return jobs.claim_next({kind: Handler()}, GATE)


@pytest.mark.parametrize("kind", PERMIT_CLASSES)
@pytest.mark.parametrize("loss", ["expired", "stolen", "recovery"])
def test_each_class_checks_live_ownership_before_call(client, dispatch_permit, kind, loss):
    claim = claim_for(kind)
    if loss == "recovery":
        change(dispatch_permit, "enter_recovery")
    else:
        with sessions()() as db, db.begin():
            job = db.get(jobs.Job, claim.id)
            if loss == "expired":
                job.lease_until = database_now(db) - timedelta(seconds=1)
            else:
                job.lease_token = uuid4()
    assert not jobs.start_call(claim, GATE)
    with pytest.raises(collector.Interrupted):
        worker._require_dispatch(claim, threading.Event())


def test_start_admission_serializes_with_recovery_but_releases_before_io(
    client, dispatch_permit, monkeypatch
):
    claim = claim_for("development_collection_v1")
    original = jobs._fenced
    observed = []

    def fenced(db, current, admission):
        # The host shared lock is already held when the live DB fence is checked.
        def writer():
            try:
                with file_lock(dispatch_permit / "permit.lock", exclusive=True, timeout=0.05):
                    observed.append("unexpected exclusive lock")
            except LockUnavailable:
                observed.append("recovery waits for admission")

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        return original(db, current, admission)

    monkeypatch.setattr(jobs, "_fenced", fenced)
    assert jobs.start_call(claim, GATE)
    assert observed == ["recovery waits for admission"]
    # A network call may now be in flight. No host lock or Job row lock remains.
    change(dispatch_permit, "enter_recovery")
    with sessions()() as db, db.begin():
        assert db.scalar(
            select(jobs.Job).where(jobs.Job.id == claim.id).with_for_update(nowait=True)
        )
    monkeypatch.setattr(jobs, "_fenced", original)
    assert not jobs.start_call(claim, GATE)
    assert not jobs.finish(claim, Handler(), error="late return", gate=GATE)


@pytest.mark.parametrize("loss", ["expired", "stolen"])
def test_obsolete_collector_makes_no_new_source_call(client, bound, loss):  # noqa: F811
    create(client, sign_in(client), incident_body(client))
    claim = jobs.claim_next(worker.HANDLERS, GATE)
    context = context_for(claim)
    with sessions()() as db, db.begin():
        row = db.get(jobs.Job, claim.id)
        if loss == "expired":
            row.lease_until = database_now(db) - timedelta(seconds=1)
        else:
            row.lease_token = uuid4()
    with pytest.raises(collector.Interrupted):
        collector.collect_checkpointed(claim, context, bound, GATE)
    assert adapters.CALLS == {}


def test_recovery_during_report_preparation_denies_actual_transport(
    permission_context, dispatch_permit, monkeypatch
):
    claim = claim_for("recommendation_report_v1", UUID(permission_context.consumer.report_id))
    original_request = reports.Request
    sent = []

    def prepare(*args, **kwargs):
        request = original_request(*args, **kwargs)
        change(dispatch_permit, "enter_recovery")
        return request

    class Transport:
        def open(self, *args, **kwargs):
            sent.append(True)
            raise AssertionError("A denied request must not reach transport")

    monkeypatch.setenv("KRR_RUNNER_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("KRR_RUNNER_KEY", raising=False)
    monkeypatch.setattr(reports, "Request", prepare)
    monkeypatch.setattr(reports, "build_opener", lambda *_args: Transport())
    with pytest.raises(collector.Interrupted):
        reports.generate(
            claim.subject_id,
            before_call=lambda: worker._require_dispatch(claim, threading.Event()),
        )
    assert sent == []


def test_recovery_during_execution_preparation_denies_gateway(
    permission_context, dispatch_permit, monkeypatch
):
    execution_id = UUID(permission_context.consumer.execution_id)
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id)
        db.get(ActionPlan, row.plan_id).expires_at = database_now(db) + timedelta(minutes=5)
    claim = claim_for("recovery_execution_v1", execution_id)
    original_now = recovery.database_now
    sent = []

    def preparing(db):
        value = original_now(db)
        change(dispatch_permit, "enter_recovery")
        return value

    monkeypatch.setattr(recovery, "database_now", preparing)
    monkeypatch.setattr(recovery.client, "gateway", lambda *_args: sent.append(True))
    with pytest.raises(collector.Interrupted):
        recovery.execute(
            execution_id, before_call=lambda: worker._require_dispatch(claim, threading.Event())
        )
    assert sent == []


def test_keeper_database_error_stops_before_the_next_source(client, bound, monkeypatch):  # noqa: F811
    create(client, sign_in(client), incident_body(client))
    failed = threading.Event()
    original_heartbeat = worker.heartbeat
    original_settings = worker.settings
    first_key = min(bound)
    original_adapter = bound[first_key]

    def heartbeat():
        if threading.current_thread() is not threading.main_thread():
            raise ConnectionError("Controlled keeper database loss")
        original_heartbeat()

    def admitted_first(context, timeout):
        assert failed.wait(5)
        return original_adapter(context, timeout)

    monkeypatch.setattr(worker, "heartbeat", heartbeat)
    monkeypatch.setattr(
        worker, "settings", lambda: replace(original_settings(), heartbeat_seconds=0.01)
    )
    monkeypatch.setattr(worker.logger, "warning", lambda *_args: failed.set())
    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, first_key, admitted_first)
    bound[first_key] = admitted_first
    assert worker.run_once()
    assert failed.is_set()
    assert adapters.CALLS == {first_key: 1}
    with sessions()() as db:
        assert db.scalar(select(jobs.Job.state)) == "running"
        assert db.scalars(select(jobs.JobCheckpoint.stage)).all() == [f"collect_source:{first_key}"]
