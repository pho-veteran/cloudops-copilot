"""Killed real worker processes and HTTP transport; gateway/Kubernetes outcomes are controlled."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from test_recovery_lifecycle import approve
from test_permissions import permission_context as _permission_context

from cloudops.features.recovery.models import Execution
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job

permission_context = _permission_context

WORKER = """
import pathlib,sys,time
from cloudops.entrypoints import worker
original=worker.execute
mode,marker=sys.argv[1],pathlib.Path(sys.argv[2])
def pause():
    marker.write_text('cutpoint reached')
    time.sleep(60)
def execute(identifier,*,before_call):
    if mode=='before_call':pause()
    count=0
    def boundary():
        nonlocal count
        before_call()
        count+=1
        if mode=='after_admission' and count==2:pause()
    result=original(identifier,before_call=boundary)
    if mode=='after_ack':pause()
    return result
worker.execute=execute
worker.promote_deferred=lambda **kwargs:None
assert worker.run_once()
"""


@pytest.mark.parametrize("cut", ["before_call", "after_admission", "after_ack"])
def test_killed_worker_takeover_keeps_one_call_admission(permission_context, tmp_path, cut):
    identifier = approve(permission_context)
    calls, receipts = [], {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer controlled-worker-key"
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/dispatch" and value["execution_id"] == str(identifier)
            with lock:
                calls.append(value)
                receipts[str(identifier)] = {
                    "outcome": "acknowledged",
                    "verification": "inconclusive",
                    "reason": "Controlled durable gateway ACK; no application verification.",
                }
            self.reply(200, receipts[str(identifier)])

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer controlled-worker-key"
            assert self.path == f"/operations/{identifier}"
            with lock:
                value = receipts.get(str(identifier))
            self.reply(200 if value else 404, value or {"error": {"code": "operation_not_found"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key = tmp_path / "dispatch-key"
    key.write_text("controlled-worker-key")
    key.chmod(0o600)
    env = {
        **os.environ,
        "EXECUTOR_URL": f"http://127.0.0.1:{server.server_port}",
        "EXECUTOR_DISPATCH_KEY": str(key),
    }
    marker = tmp_path / "cutpoint"
    with (tmp_path / "worker.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", WORKER, cut, str(marker)], env=env, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not marker.exists():
                log.seek(0)
                pytest.fail("Worker did not reach cutpoint: " + log.read()[-2000:])
            process.kill()
            process.wait(timeout=5)
            with sessions()() as db:
                row = db.get(Execution, identifier)
                request, started, reconciliation_deadline = (
                    row.dispatch_request,
                    row.dispatch_started_at,
                    row.reconcile_deadline,
                )
                assert (started is None) == (cut == "before_call")
            # The generic lease test covers real elapsed expiry; this test controls takeover timing.
            with sessions()() as db, db.begin():
                job = db.scalar(
                    select(Job).where(
                        Job.kind == "recovery_execution_v1", Job.subject_id == identifier
                    )
                )
                job.lease_until = database_now(db) - timedelta(seconds=1)
            replay = "from cloudops.entrypoints import worker; worker.promote_deferred=lambda **kwargs:None; assert worker.run_once()"
            result = subprocess.run(
                [sys.executable, "-c", replay], env=env, stdout=log, stderr=log, timeout=15
            )
            assert result.returncode == 0
            with sessions()() as db:
                row = db.get(Execution, identifier)
                assert row.dispatch_request == request
                if cut == "after_admission":
                    assert calls == [] and row.state == "reconciling" and row.holds_lock
                    assert row.outcome.attribution == "outcome_unknown"
                else:
                    assert (
                        len(calls) == 1
                        and row.state == "manual_intervention"
                        and not row.holds_lock
                    )
                    assert row.outcome.attribution == "acknowledged"
                if started is not None:
                    assert (
                        row.dispatch_started_at == started
                        and row.reconcile_deadline == reconciliation_deadline
                    )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
