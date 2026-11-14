"""Real auth/SQL/worker with explicitly controlled blocking reader and executor fixtures."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from sqlalchemy import func, select
from test_consumers import decide
from test_permissions import permission_context as _permission_context
from test_workload_observation import live_workloads as _live_workloads

from cloudops.entrypoints.worker import run_once
from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recovery import client as gateway_client
from cloudops.features.recovery.models import Execution
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_models import ObservationState
from cloudops.features.workloads.observation_service import observe_direct
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job

permission_context = _permission_context
live_workloads = _live_workloads


def test_blocked_observer_leases_do_not_occupy_incident_or_approved_recovery_worker(
    permission_context, live_workloads, monkeypatch
):
    context = permission_context
    release, all_started = threading.Event(), threading.Event()
    started = []
    dispatched = []

    class BlockedReader:
        async def read(self, identity, started_at):
            started.append(identity.workload_id)
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(asyncio.to_thread(release.wait), timeout=10)
            raise ObservationError("cluster_unavailable")

    async def blocked_reads():
        return await asyncio.gather(
            *[
                observe_direct(identity, "scheduled", reader=BlockedReader())
                for identity in live_workloads[:3]
            ]
        )

    def dispatch(path, body):
        assert path == "/dispatch" and not release.is_set()
        dispatched.append(body["execution_id"])
        return {"outcome": "acknowledged", "verification": "controlled_executor_fixture"}

    monkeypatch.setattr(gateway_client, "gateway", dispatch)
    with sessions()() as db, db.begin():
        # The seeded recovery consumer is the hundredth active live identity.
        db.get(Workload, live_workloads[-1]).active = False
        # The deferred cancellation fixture would be promoted into this bounded job receipt.
        pending = db.get(InvestigationRun, UUID(context.consumer.pending_run_id))
        pending.state, pending.queue_reason, pending.outcome_code = (
            "cancelled",
            None,
            "operator_cancelled",
        )
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(asyncio.run, blocked_reads())
        try:
            assert all_started.wait(5)
            assert settings().queue_limit == 100
            actor = context.actors["operator"]
            admission = actor.client.post(
                "/api/incidents",
                json=context.body,
                headers={**actor.headers, "Idempotency-Key": str(uuid4())},
            )
            assert admission.status_code == 201
            assert decide(context).status_code == 200
            assert run_once() and run_once()
            assert dispatched and not release.is_set()
            with sessions()() as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(ObservationState)
                        .where(ObservationState.lease_until > database_now(db))
                    )
                    == 3
                )
                execution = db.scalar(
                    select(Execution).where(Execution.plan_id == UUID(context.consumer.plan_id))
                )
                assert (
                    execution.id == UUID(dispatched[0]) and execution.state == "manual_intervention"
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(Job.state.in_(["queued", "running"]), Job.kind != ANALYSIS_JOB)
                    )
                    == 0
                )
                # Collection now admits a later analysis phase. It must stay behind
                # the already approved recovery in the common FIFO, without a claim.
                model_job = db.scalar(
                    select(Job).where(Job.kind == ANALYSIS_JOB, Job.state == "queued")
                )
                assert model_job is not None and model_job.attempts == 0
        finally:
            release.set()
        assert all(result.outcome == "failed" for result in running.result(timeout=5))
