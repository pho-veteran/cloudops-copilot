import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from conftest import sign_in
from sqlalchemy import select, update
from test_access_admission import create, incident_body

from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.investigations.collector import collect, prepare
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, claim_next, finish, renew


def queued(client):
    headers = sign_in(client)
    response = create(client, headers, incident_body(client))
    assert response.status_code == 201
    return response.json(), headers


def expire(claim):
    with sessions()() as db, db.begin():
        db.execute(
            update(Job)
            .where(Job.id == claim.id)
            .values(lease_until=database_now(db) - timedelta(seconds=1))
        )


def read_run(client, ids):
    return client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()


def test_collection_persists_labeled_immutable_evidence(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    ids, headers = queued(client)
    assert read_run(client, ids)["state"] == "queued"
    assert run_once()
    result = read_run(client, ids)
    assert result["state"] == "insufficient_evidence"
    assert result["diagnosis"] == result["recovery"] == "unavailable"
    assert result["attempts"][0]["state"] == "succeeded"
    evidence = result["evidence"][0]
    assert evidence["source"] == "development_fixture"
    assert evidence["workload_uid"] == "development-checkout-v1"
    assert (
        evidence["sha256"]
        == hashlib.sha256(
            json.dumps(evidence["payload"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    again = client.post(
        f"/api/incidents/{ids['incident_id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 201
    assert read_run(client, ids)["evidence"] == result["evidence"]
    assert len(client.get(f"/api/incidents/{ids['incident_id']}/runs").json()["items"]) == 2


def test_restart_takeover_fences_obsolete_and_duplicate_results(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    ids, _ = queued(client)
    old = claim_next(HANDLERS)
    old_result = collect(prepare(old.subject_id))
    assert renew(old)
    expire(old)
    assert not renew(old)
    assert not finish(old, HANDLERS[old.kind], result=old_result)
    # A fresh worker/DB session reclaims the persisted job, as after process loss.
    new = claim_next(HANDLERS)
    assert new.attempt == 2 and new.token != old.token
    assert not finish(old, HANDLERS[old.kind], result=old_result)
    assert finish(new, HANDLERS[new.kind], result=collect(prepare(new.subject_id)))
    assert not finish(new, HANDLERS[new.kind], result=old_result)
    result = read_run(client, ids)
    assert [a["state"] for a in result["attempts"]] == ["expired", "succeeded"]
    assert len(result["evidence"]) == 1


def test_failures_exhaust_budget_without_success_evidence(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    monkeypatch.setenv("COLLECTOR_FAIL", "true")
    settings.cache_clear()
    ids, headers = queued(client)
    for _ in range(3):
        assert run_once()
    assert not run_once()
    result = read_run(client, ids)
    assert result["state"] == "failed" and result["evidence"] == []
    assert [a["state"] for a in result["attempts"]] == ["failed"] * 3
    assert (
        client.post(
            f"/api/incidents/{ids['incident_id']}/runs",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 201
    )
    assert read_run(client, ids)["state"] == "failed"


def test_repeated_worker_loss_exhausts_budget(client):
    ids, _ = queued(client)
    for _ in range(3):
        claim = claim_next(HANDLERS)
        expire(claim)
    assert claim_next(HANDLERS) is None
    result = read_run(client, ids)
    assert result["state"] == "failed" and result["evidence"] == []
    assert [a["state"] for a in result["attempts"]] == ["expired"] * 3


def test_concurrent_claim_has_single_owner(client):
    queued(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: claim_next(HANDLERS), range(4)))
    assert len([c for c in claims if c]) == 1


def test_failed_publication_rolls_back_output_and_completion(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    ids, _ = queued(client)
    claim = claim_next(HANDLERS)

    class BrokenHandler:
        def publish(self, db, subject_id, result):
            HANDLERS[claim.kind].publish(db, subject_id, result)
            db.flush()
            raise RuntimeError("commit interrupted")

    with pytest.raises(RuntimeError):
        finish(claim, BrokenHandler(), result=collect(prepare(claim.subject_id)))
    assert read_run(client, ids)["evidence"] == []
    with sessions()() as db:
        assert db.get(Job, claim.id).state == "running"
    expire(claim)
    assert run_once()
    assert len(read_run(client, ids)["evidence"]) == 1


def test_worker_in_fresh_process_loads_all_persistence(client, monkeypatch):
    import os
    import subprocess
    import sys

    ids, _ = queued(client)
    environment = {**os.environ, "COLLECTOR_DELAY_SECONDS": "0", "COLLECTOR_FAIL": "false"}
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from cloudops.entrypoints.worker import run_once; assert run_once()",
        ],
        env=environment,
        check=True,
        timeout=20,
        capture_output=True,
    )
    result = read_run(client, ids)
    assert result["state"] == "insufficient_evidence"
    assert len(result["evidence"]) == 1


def test_killed_worker_is_reclaimed_after_real_lease_expiry(client):
    import os
    import subprocess
    import sys
    import time

    ids, _ = queued(client)
    environment = {**os.environ, "COLLECTOR_DELAY_SECONDS": "60", "COLLECTOR_FAIL": "false"}
    process = subprocess.Popen(
        [sys.executable, "-c", "from cloudops.entrypoints.worker import run_once; run_once()"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        lease_remaining = None
        while time.monotonic() < deadline:
            with sessions()() as db:
                job = db.scalar(select(Job).where(Job.subject_id == UUID(ids["run_id"])))
                if job.state == "running":
                    lease_remaining = (job.lease_until - database_now(db)).total_seconds()
                    break
            time.sleep(0.05)
        assert lease_remaining is not None, "Worker did not claim the persisted job."
        process.kill()
        process.wait(timeout=5)
        assert read_run(client, ids)["state"] == "collecting"
        # Let the actual database lease expire, without editing its deadline.
        time.sleep(max(0, lease_remaining) + 0.2)
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from cloudops.entrypoints.worker import run_once; assert run_once()",
            ],
            env={**environment, "COLLECTOR_DELAY_SECONDS": "0"},
            check=True,
            timeout=10,
            capture_output=True,
        )
        result = read_run(client, ids)
        assert [a["state"] for a in result["attempts"]] == ["expired", "succeeded"]
        assert len(result["evidence"]) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
