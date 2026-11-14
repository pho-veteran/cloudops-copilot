"""Real worker-process interruption and wire boundaries; all fault hooks are test-only."""

import os
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest

from diagnosis_fake import respond, serve
from diagnosis_fixtures import text_payload
from sqlalchemy import select
from test_diagnosis_lifecycle import admission, deployment as _deployment, refresh
from test_diagnosis_validation import _crashloop, _raw, context as _context

from cloudops.features.diagnosis.models import AnalysisContext, AnalysisResult
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import database_now
from cloudops.shared.jobs import Job

context, deployment = _context, _deployment


def start(code="", *args):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            code or "from cloudops.entrypoints.worker import run_once; run_once()",
            *args,
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def finish(process):
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, (out.decode(errors="replace"), err.decode(errors="replace"))


def test_two_process_slot_and_kill_unknown_no_replay(db, context, deployment):
    _crashloop(db, context)
    entered, release = threading.Event(), threading.Event()

    def handler(h):
        entered.set()
        release.wait(30)
        try:
            respond(h, text_payload(_raw()))
        except OSError:
            pass

    with serve(handler) as server:
        deployment(server.url)
        identifier = admission(db, context)
        first = start()
        try:
            assert entered.wait(20), "First worker did not reach the real wire boundary."
            # A real collection backlog must still progress while another process owns the slot.
            original = db.get(InvestigationRun, context[1])
            old_incident = db.get(Incident, context[0])
            incident = Incident(
                workload_id=old_incident.workload_id,
                title="Collection backlog",
                description="Owned process fixture",
                created_by=original.requested_by,
            )
            db.add(incident)
            db.flush()
            collecting = InvestigationRun(
                incident_id=incident.id,
                requested_by=original.requested_by,
                question=original.question,
                question_source=original.question_source,
                window_start=original.window_start,
                window_end=original.window_end,
                window_source=original.window_source,
            )
            db.add(collecting)
            db.flush()
            db.add(Job(kind="development_collection_v1", subject_id=collecting.id))
            db.commit()
            second = start()
            finish(second)
            db.expire_all()
            assert db.get(InvestigationRun, collecting.id).pack_coverage is not None
            assert db.scalar(select(Job).where(Job.subject_id == identifier)).attempts == 1
            assert len(server.requests) == 1
        finally:
            first.terminate()
            first.communicate(timeout=10)
            release.set()
        db.expire_all()
        job = db.scalar(select(Job).where(Job.subject_id == identifier))
        job.lease_until = database_now(db) - timedelta(seconds=1)
        db.commit()
        finish(start())
        assert refresh(db, identifier).reason == "provider_outcome_unknown"
        assert db.get(AnalysisResult, identifier) is None
        assert len(server.requests) == 1


def test_kill_after_context_commit_reuses_exact_snapshot(db, context, deployment, tmp_path):
    _crashloop(db, context)
    marker = tmp_path / "context-committed"
    code = """
import sys,time
from pathlib import Path
from cloudops.features.diagnosis import worker
from cloudops.entrypoints.worker import run_once
original=worker._prepare
def prepare(*args):
    value=original(*args)
    Path(sys.argv[1]).write_text('committed')
    time.sleep(60)
    return value
worker._prepare=prepare
run_once()
"""
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        process = start(code, str(marker))
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                threading.Event().wait(0.05)
            assert marker.exists(), "Worker failed before durable context boundary."
            db.expire_all()
            saved = db.get(AnalysisContext, identifier).snapshot
            assert not server.requests
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        db.expire_all()
        db.scalar(select(Job).where(Job.subject_id == identifier)).lease_until = database_now(
            db
        ) - timedelta(seconds=1)
        db.commit()
        finish(start())
        assert refresh(db, identifier).state == "completed"
        assert db.get(AnalysisContext, identifier).snapshot is None
        assert db.get(AnalysisContext, identifier).digest == saved["digest"]
        import json

        assert json.loads(server.requests[0]["body"])["messages"] == saved["messages"]
        assert len(server.requests) == 1


@pytest.mark.parametrize("boundary", ["validated_before_settle", "after_atomic_settle"])
def test_final_attempt_process_kill_has_no_receipt_only_gap(
    db, context, deployment, tmp_path, boundary
):
    _crashloop(db, context)
    marker = tmp_path / "boundary"
    code = """
import sys,time
from pathlib import Path
from cloudops.entrypoints import worker
from cloudops.features.diagnosis import advice
if sys.argv[2]=='validated_before_settle':
    original=advice.validate
    def validate(*args):
        result=original(*args)
        Path(sys.argv[1]).write_text('validated')
        time.sleep(60)
        return result
    advice.validate=validate
else:
    original=worker.analyze
    def analyze(*args):
        original(*args)
        Path(sys.argv[1]).write_text('settled')
        time.sleep(60)
    worker.analyze=analyze
worker.run_once()
"""
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        db.scalar(select(Job).where(Job.subject_id == identifier)).attempts = 2
        db.commit()
        process = start(code, str(marker), boundary)
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                threading.Event().wait(0.05)
            assert marker.exists()
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        db.expire_all()
        job = db.scalar(select(Job).where(Job.subject_id == identifier))
        if boundary == "validated_before_settle":
            assert db.get(AnalysisResult, identifier) is None
            job.lease_until = database_now(db) - timedelta(seconds=1)
            db.commit()
            finish(start())
            assert refresh(db, identifier).reason == "provider_outcome_unknown"
        else:
            assert job.state == "completed"
            assert db.get(AnalysisResult, identifier) is not None
            finish(start())
            assert refresh(db, identifier).state == "completed"
        db.expire_all()
        assert db.scalar(select(Job).where(Job.subject_id == identifier)).attempts == 3
        assert len(server.requests) == 1
