"""Durable per-source collection checkpoints: takeover, fencing and shared attempt budget."""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import checkpoint_adapters as adapters
import pytest
from conftest import sign_in, wait_for_job_lease_expiry
from sqlalchemy import func, select
from test_access_admission import create, incident_body
from test_jobs import expire

from cloudops.entrypoints import worker
from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.investigations import collector
from cloudops.features.investigations.catalog import SOURCES
from cloudops.features.investigations.collector import (
    FinalizeCollection,
    Interrupted,
    collect_checkpointed,
    prepare,
)
from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.jobs import Job, JobAttempt, JobCheckpoint, checkpoint, claim_next, finish

TESTS = Path(__file__).resolve().parent


class ProcessLost(BaseException):
    """Simulates abrupt worker loss: nothing after it runs, not even finish(error)."""


@pytest.fixture
def bound(monkeypatch):
    """Register labeled test adapters for state, events and current logs plus the fixture."""
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    adapters.CALLS.clear()
    for key, adapter in adapters.ADAPTERS.items():
        monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, adapter)
    binding = dict(adapters.ADAPTERS)
    monkeypatch.setattr(worker, "configured_adapters", lambda _mode: binding)
    return binding


def run_state(client, ids):
    return client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()


def evidence_ids(ids):
    with sessions()() as db:
        return {
            row.source_key: (row.id, row.sha256)
            for row in db.scalars(select(Evidence).where(Evidence.run_id == UUID(ids["run_id"])))
        }


def context_for(claim):
    return prepare(claim.subject_id, attempt_number=claim.attempt, max_attempts=claim.max_attempts)


def crash_at(monkeypatch, bound, key, claim, context=None):
    """Run a checkpointed attempt whose read of ``key`` dies with the worker process."""
    original = bound[key]

    def lost(_context, _timeout):
        raise ProcessLost()

    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, lost)
    try:
        with pytest.raises(ProcessLost):
            collect_checkpointed(claim, context or context_for(claim), {**bound, key: lost}, GATE)
    finally:
        monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, original)


def test_takeover_reuses_committed_sources_with_the_same_ids(client, bound, monkeypatch):
    ids = create(client, sign_in(client), incident_body(client)).json()
    first = claim_next(HANDLERS, GATE)
    ordered = sorted(bound)
    crash_at(monkeypatch, bound, ordered[3], first)
    before = evidence_ids(ids)
    assert len(before) == 3 and set(before) == set(ordered[:3])
    view = run_state(client, ids)
    assert view["state"] == "collecting" and view["pack_coverage"] is None
    assert view["progress"]["sources_committed"] == 3 and not view["progress"]["finalized"]
    assert view["cancel"] == {"allowed": False, "reason": "collection_started"}

    expire(first)
    second = claim_next(HANDLERS, GATE)
    assert second.attempt == 2
    result = collect_checkpointed(second, context_for(second), bound, GATE)
    assert adapters.CALLS == {
        "kubernetes.state": 1,
        "kubernetes.events": 1,
        "kubernetes.logs.current": 1,
        "development_fixture": 1,
    }
    assert finish(second, HANDLERS[second.kind], result=result, gate=GATE)
    after = evidence_ids(ids)
    assert set(after) == set(SOURCES)
    assert {key: after[key] for key in before} == before
    view = run_state(client, ids)
    assert (view["state"], view["outcome_code"]) == (
        "insufficient_evidence",
        "analysis_unavailable",
    )
    assert [(a["state"], a["sources_committed"]) for a in view["attempts"]] == [
        ("expired", 3),
        ("succeeded", 1),
    ]
    assert view["progress"] == {
        "sources_total": len(SOURCES),
        "sources_committed": len(SOURCES),
        "sources_reused": 3,
        "finalized": True,
        "attempt": 2,
        "max_attempts": 3,
    }
    with sessions()() as db:
        stages = db.scalars(select(JobCheckpoint.stage).order_by(JobCheckpoint.sequence)).all()
    assert stages[-1] == "finalize_collection" and len(stages) == 5


def test_obsolete_claim_cannot_checkpoint_or_finalize(client, bound, monkeypatch):
    ids = create(client, sign_in(client), incident_body(client)).json()
    old = claim_next(HANDLERS, GATE)
    context = context_for(old)
    crash_at(monkeypatch, bound, "kubernetes.events", old, context)
    committed = evidence_ids(ids)
    expire(old)
    new = claim_next(HANDLERS, GATE)
    late = adapters.events(context, 5)

    def write(db, carry):
        from cloudops.features.investigations.storage import store_source

        return store_source(db, old.subject_id, context.workload_uid, late, carry)

    assert not checkpoint(old, "collect_source:kubernetes.events", GATE, write)
    assert not finish(old, HANDLERS[old.kind], result=FinalizeCollection("x"), gate=GATE)
    assert evidence_ids(ids) == committed
    with pytest.raises(Interrupted):
        collect_checkpointed(old, context, bound, GATE)
    assert finish(
        new,
        HANDLERS[new.kind],
        result=collect_checkpointed(new, context_for(new), bound, GATE),
        gate=GATE,
    )
    assert {k: evidence_ids(ids)[k] for k in committed} == committed


def test_transient_source_failure_resumes_inside_the_shared_three_attempt_budget(
    client, bound, monkeypatch
):
    ids = create(client, sign_in(client), incident_body(client)).json()
    calls = []

    def flaky(_context, _timeout):
        calls.append(True)
        raise collector.SourceTimeout()

    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, "kubernetes.events", flaky)
    bound["kubernetes.events"] = flaky
    assert run_once() and run_once() and run_once()
    assert not run_once()
    view = run_state(client, ids)
    assert [a["state"] for a in view["attempts"]] == ["failed", "failed", "succeeded"]
    assert len(calls) == 3 and adapters.CALLS["kubernetes.state"] == 1
    events = next(row for row in view["evidence"] if row["source_key"] == "kubernetes.events")
    assert events["coverage"] == {"status": "unavailable", "reason_codes": ["timeout"]}
    assert view["state"] == "insufficient_evidence"


@pytest.mark.parametrize("metrics", [False, True], ids=["pod-budget", "sample-budget"])
def test_batch_checkpoint_and_takeover_preserve_identical_bounded_content(
    client, bound, monkeypatch, metrics
):
    if metrics:
        for key, adapter in adapters.METRIC_ADAPTERS.items():
            monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, adapter)
            bound[key] = adapter
    headers = sign_in(client)

    def admitted(title):
        body = incident_body(client)
        body["title"] = title
        ids = create(client, headers, body).json()
        return ids, claim_next(HANDLERS, GATE)

    batch, claim = admitted("Batch baseline")
    assert finish(
        claim,
        HANDLERS[claim.kind],
        result=collector.collect(context_for(claim), bound),
        gate=GATE,
    )
    straight, claim = admitted("Checkpointed collection")
    assert finish(
        claim,
        HANDLERS[claim.kind],
        result=collect_checkpointed(claim, context_for(claim), bound, GATE),
        gate=GATE,
    )
    resumed, first = admitted("Checkpointed takeover")
    crash_at(monkeypatch, bound, sorted(bound)[3], first)
    before = evidence_ids(resumed)
    assert len(before) == 3
    expire(first)
    second = claim_next(HANDLERS, GATE)
    assert finish(
        second,
        HANDLERS[second.kind],
        result=collect_checkpointed(second, context_for(second), bound, GATE),
        gate=GATE,
    )
    assert {key: evidence_ids(resumed)[key] for key in before} == before

    def stored(ids):
        with sessions()() as db:
            return {
                key: (digest, coverage, payload)
                for key, digest, coverage, payload in db.execute(
                    select(
                        Evidence.source_key, Evidence.sha256, Evidence.coverage, Evidence.payload
                    ).where(Evidence.run_id == UUID(ids["run_id"]))
                )
            }

    expected = stored(batch)
    assert stored(straight) == expected
    assert stored(resumed) == expected
    if metrics:
        early = expected["prometheus.error_ratio"]
        late = expected["prometheus.request_rate"]
        assert len(early[2]["series"][0]["samples"]) == 12000
        assert len(late[2]["series"][0]["samples"]) == 8000
        assert early[1] == {"status": "available", "reason_codes": []}
        assert late[1] == {"status": "partial", "reason_codes": ["truncated"]}
    else:
        state = expected["kubernetes.state"]
        logs = expected["kubernetes.logs.current"]
        pods = {pod["uid"] for row in (state, logs) for pod in row[2]["pods"]}
        assert len(pods) == 10
        assert state[1]["reason_codes"] == ["truncated"]


def test_failed_finalization_rolls_back_and_takeover_finalizes_once(client, bound):
    ids = create(client, sign_in(client), incident_body(client)).json()
    claim = claim_next(HANDLERS, GATE)
    result = collect_checkpointed(claim, context_for(claim), bound, GATE)

    class Broken:
        def publish(self, db, subject_id, value):
            HANDLERS[claim.kind].publish(db, subject_id, value)
            db.flush()
            raise RuntimeError("commit interrupted")

    with pytest.raises(RuntimeError):
        finish(claim, Broken(), result=result, gate=GATE)
    view = run_state(client, ids)
    assert view["state"] == "collecting" and view["pack_coverage"] is None
    assert len(view["evidence"]) == 4
    expire(claim)
    adapters.CALLS.clear()
    second = claim_next(HANDLERS, GATE)
    final = collect_checkpointed(second, context_for(second), bound, GATE)
    assert adapters.CALLS == {}
    assert finish(second, HANDLERS[second.kind], result=final, gate=GATE)
    with sessions()() as db:
        pins = db.scalars(select(EvidencePin.evidence_id).where(EvidencePin.kind == "latest")).all()
        finals = db.scalar(
            select(func.count())
            .select_from(JobCheckpoint)
            .where(JobCheckpoint.stage == "finalize_collection")
        )
    useful = {
        UUID(row["id"])
        for row in run_state(client, ids)["evidence"]
        if row["content_status"] == "available"
    }
    assert set(pins) == useful and len(pins) == len(useful) and finals == 1


def test_unknown_job_kind_is_never_claimed(client):
    with sessions()() as db, db.begin():
        db.add(Job(kind="unregistered_v1", subject_id=UUID(int=7)))
    assert not run_once()
    with sessions()() as db:
        job = db.scalar(select(Job).where(Job.kind == "unregistered_v1"))
        assert (job.state, job.attempts, job.lease_token, job.lease_until) == (
            "queued",
            0,
            None,
            None,
        )
        assert db.scalar(select(func.count()).select_from(JobAttempt)) == 0


def _poll(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Condition not reached.")


def test_real_worker_killed_after_a_committed_checkpoint_resumes(client):
    """An actual worker process dies between checkpoints; a fresh process resumes it."""
    ids = create(client, sign_in(client), incident_body(client)).json()
    script = (
        "import checkpoint_adapters as a; from cloudops.entrypoints import worker; "
        "binding = a.install(); worker.configured_adapters = lambda _m: binding; "
        "worker.run_once()"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(TESTS.parent / "src"), str(TESTS))),
        "COLLECTOR_DELAY_SECONDS": "0",
        "COLLECTOR_FAIL": "false",
        "CHECKPOINT_FIXTURE_DELAY": "60",
        "CHECKPOINT_DELAY_SOURCE": sorted(adapters.ADAPTERS)[3],
    }
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:

        def committed():
            with sessions()() as db:
                return db.scalar(select(func.count()).select_from(JobCheckpoint)) >= 3

        _poll(committed)
        process.kill()
        process.wait(timeout=5)
        before = evidence_ids(ids)
        assert len(before) == 3 and run_state(client, ids)["state"] == "collecting"
        # Wait for the actual database lease to expire, without editing its deadline.
        wait_for_job_lease_expiry(UUID(ids["run_id"]))
        subprocess.run(
            [sys.executable, "-c", script.replace("worker.run_once()", "assert worker.run_once()")],
            env={**environment, "CHECKPOINT_FIXTURE_DELAY": "0"},
            check=True,
            timeout=30,
            capture_output=True,
        )
        view = run_state(client, ids)
        assert [a["state"] for a in view["attempts"]] == ["expired", "succeeded"]
        assert view["progress"]["sources_reused"] == 3
        after = evidence_ids(ids)
        assert {key: after[key] for key in before} == before and set(after) == set(SOURCES)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_lost_lease_stops_before_the_next_external_call(client, bound):
    create(client, sign_in(client), incident_body(client))
    claim = claim_next(HANDLERS, GATE)
    import threading

    lost = threading.Event()
    lost.set()
    with pytest.raises(Interrupted):
        collect_checkpointed(claim, context_for(claim), bound, GATE, lost)
    assert adapters.CALLS == {}
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(JobCheckpoint)) == 0
