"""A capacity reservation survives replacement of the worker process."""

import json
import os
import subprocess
import sys

from conftest import sign_in
from sqlalchemy import func, select
from test_access_admission import incident_body
from test_incident_requests import (
    automatic,
    capacity as _capacity,
    free_capacity,
    identities as _identities,
)

from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import sessions
from cloudops.shared.config import settings
from cloudops.shared.jobs import Job, JobAttempt

capacity = _capacity
identities = _identities


def test_deferred_run_is_promoted_by_a_replacement_worker_process(client, capacity, identities):
    sign_in(client)
    workload = incident_body(client)["workload_id"]
    dispatch_limit = settings().queue_limit
    capacity(jobs=dispatch_limit, deferred=1, filled=dispatch_limit)
    accepted = automatic(identities, "restart-episode", workload)
    environment = {
        **os.environ,
        "DEFERRED_RUN_LIMIT": "1",
        "COLLECTOR_DELAY_SECONDS": "0",
        "COLLECTOR_FAIL": "false",
    }

    def worker_process():
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, os; from cloudops.entrypoints.worker import run_once; "
                "print(json.dumps({'worked': run_once(), 'pid': os.getpid()}))",
            ],
            env=environment,
            check=True,
            timeout=20,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    first = worker_process()
    assert first["worked"] is False
    with sessions()() as db:
        waiting = db.get(InvestigationRun, accepted.run_id)
        assert (waiting.state, waiting.queue_reason) == ("queued", "capacity")
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.subject_id == waiting.id))
            == 0
        )
    free_capacity()
    # No new admission/alert or Operator Run action occurs between these processes.
    second = worker_process()
    assert second["pid"] != first["pid"] and second["worked"] is True
    with sessions()() as db:
        completed = db.get(InvestigationRun, accepted.run_id)
        assert completed.state == "insufficient_evidence" and completed.queue_reason is None
        job = db.scalar(select(Job).where(Job.subject_id == accepted.run_id))
        assert job.attempts == 1 and job.state == "completed"
        assert (
            db.scalar(
                select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job.id)
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(InvestigationRun)) == 1
