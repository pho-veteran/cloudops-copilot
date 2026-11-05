"""Real row-lock interleavings cannot commit a payload pin after purge."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event
from uuid import uuid4

from sqlalchemy import delete, event, select, text, update
from test_evidence_boundary import batch, setup_run

from cloudops.features.incidents.models import Incident
from cloudops.features.investigations import retention
from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.features.investigations.storage import pin_resolution, store_batch
from cloudops.shared.database import engine, sessions


def candidate(client):
    incident_id, run_id, uid = setup_run(client)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    with sessions()() as db, db.begin():
        rows = store_batch(db, run_id, uid, batch(datetime.now(timezone.utc)))
        evidence_id = next(row.id for row in rows if row.source_key == "development_fixture")
        db.execute(delete(EvidencePin).where(EvidencePin.evidence_id == evidence_id))
        db.execute(update(Evidence).where(Evidence.id == evidence_id).values(expires_at=expires_at))
    return incident_id, evidence_id, expires_at


def await_database_lock(pid, future):
    deadline = time.monotonic() + 10
    with engine().connect() as monitor:
        while True:
            monitor.execute(text("SELECT pg_stat_clear_snapshot()"))
            if monitor.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE pid=:pid AND wait_event_type='Lock')"
                ),
                {"pid": pid},
            ):
                return
            assert not future.done(), "Operation escaped the competing transaction's row lock."
            assert time.monotonic() < deadline, "Operation never waited on the evidence row."
            time.sleep(0.01)


def test_successful_resolution_pin_wins_before_expiry_and_purge_rechecks(client, monkeypatch):
    incident_id, evidence_id, expires_at = candidate(client)
    # The pin starts while content is valid; the competing maintenance operation
    # evaluates expiry after the deadline. No wall-clock wait is required.
    monkeypatch.setattr(retention, "database_now", lambda db: expires_at + timedelta(seconds=1))
    holder = sessions()()
    backend_pid = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            holder.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
            pin_resolution(holder, incident_id, uuid4())
            holder.flush()

            def purge():
                with sessions()() as db:
                    backend_pid.put(db.scalar(text("SELECT pg_backend_pid()")))
                    return retention.purge_expired(db, apply=True)

            purging = pool.submit(purge)
            await_database_lock(backend_pid.get(timeout=10), purging)
            holder.commit()
            assert purging.result(timeout=10) == 0
        finally:
            holder.close()
    with sessions()() as db:
        assert (
            db.scalar(
                select(EvidencePin.evidence_id).where(
                    EvidencePin.evidence_id == evidence_id, EvidencePin.kind == "resolution"
                )
            )
            == evidence_id
        )
        assert db.get(Evidence, evidence_id).payload is not None


def test_purge_wins_then_resolution_projects_tombstone_without_false_pin(client, monkeypatch):
    incident_id, evidence_id, expires_at = candidate(client)
    monkeypatch.setattr(retention, "database_now", lambda db: expires_at + timedelta(seconds=1))
    purge_ready, release_purge = Event(), Event()
    backend_pid = Queue()

    def purge():
        with sessions()() as db:

            def before_commit(session):
                purge_ready.set()
                assert release_purge.wait(10), "Competing pin never reached the held purge."

            event.listen(db, "before_commit", before_commit)
            return retention.purge_expired(db, apply=True)

    def pin():
        with sessions()() as db, db.begin():
            backend_pid.put(db.scalar(text("SELECT pg_backend_pid()")))
            db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
            pin_resolution(db, incident_id, uuid4())

    with ThreadPoolExecutor(max_workers=2) as pool:
        purging = pool.submit(purge)
        try:
            assert purge_ready.wait(10), "Purge did not reach its uncommitted update."
            pinning = pool.submit(pin)
            await_database_lock(backend_pid.get(timeout=10), pinning)
        finally:
            release_purge.set()
        assert purging.result(timeout=10) == 1
        pinning.result(timeout=10)
    with sessions()() as db:
        assert (
            db.scalar(select(EvidencePin.evidence_id).where(EvidencePin.evidence_id == evidence_id))
            is None
        )
        row = db.get(Evidence, evidence_id)
        assert row.payload is None and row.sha256 is not None
