from datetime import datetime
from typing import get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.connections.operations_schemas import (
    BlockedRun,
    JobCounts,
    JobKind,
    JobKindStatus,
    OperationsView,
    QuotaWaitingRun,
)
from cloudops.features.diagnosis.models import Analysis
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.states import COLLECTION_JOB
from cloudops.shared.database import database_now
from cloudops.shared.dispatch_permit import DispatchGate
from cloudops.shared.jobs import Job

JOB_KINDS = get_args(JobKind)
JOB_READ_LIMIT = 1000
RUN_SAMPLE_LIMIT = 20


def active_jobs_query():
    # The extra row detects overflow. No partial counts or ages are published in that case.
    return (
        select(
            Job.kind,
            Job.state,
            Job.created_at,
            Job.due_at,
            Job.lease_until,
            Job.attempts,
            Job.max_attempts,
        )
        .where(Job.state.in_(("queued", "running", "held")), Job.kind.in_(JOB_KINDS))
        .limit(JOB_READ_LIMIT + 1)
    )


def capacity_runs_query():
    has_collection_job = (
        select(Job.id)
        .where(Job.kind == COLLECTION_JOB, Job.subject_id == InvestigationRun.id)
        .exists()
    )
    return (
        select(
            InvestigationRun.incident_id,
            Incident.title.label("incident_title"),
            InvestigationRun.id.label("run_id"),
            InvestigationRun.created_at,
        )
        .join(Incident, Incident.id == InvestigationRun.incident_id)
        .where(
            InvestigationRun.state == "queued",
            InvestigationRun.queue_reason == "capacity",
            ~has_collection_job,
        )
        .order_by(InvestigationRun.created_at, InvestigationRun.id)
        .limit(RUN_SAMPLE_LIMIT + 1)
    )


def quota_runs_query():
    earliest_retry = (
        select(Analysis.earliest_retry)
        .where(
            Analysis.run_id == InvestigationRun.id,
            Analysis.purpose == "operational",
            Analysis.state == "waiting_for_quota",
        )
        .scalar_subquery()
    )
    return (
        select(
            InvestigationRun.incident_id,
            Incident.title.label("incident_title"),
            InvestigationRun.id.label("run_id"),
            InvestigationRun.created_at,
            earliest_retry.label("earliest_retry_at"),
        )
        .join(Incident, Incident.id == InvestigationRun.incident_id)
        .where(InvestigationRun.state == "waiting_for_quota")
        .order_by(InvestigationRun.created_at, InvestigationRun.id)
        .limit(RUN_SAMPLE_LIMIT + 1)
    )


def summarize_jobs(rows, now: datetime) -> tuple[dict[str, JobCounts], int | None]:
    counts = {
        kind: JobCounts(
            queued=0,
            running=0,
            held=0,
            runnable=0,
            due_retry=0,
            future_retry=0,
            expired_lease=0,
            earliest_future_retry_at=None,
        )
        for kind in JOB_KINDS
    }
    oldest = None
    for row in rows:
        status = counts[row.kind]
        if row.state == "held":
            status.held += 1
        elif row.state == "running":
            status.running += 1
            if row.lease_until is None:
                status.expired_lease = None
            elif row.lease_until <= now and status.expired_lease is not None:
                status.expired_lease += 1
        else:
            status.queued += 1
            # Match the scheduler's due-time boundary. Dispatch authority is a separate signal.
            if row.due_at is not None and row.due_at > now:
                status.future_retry += 1
                if (
                    status.earliest_future_retry_at is None
                    or row.due_at < status.earliest_future_retry_at
                ):
                    status.earliest_future_retry_at = row.due_at
            elif row.attempts < row.max_attempts:
                status.runnable += 1
                status.due_retry += int(row.attempts > 0)
                if oldest is None or row.created_at < oldest:
                    oldest = row.created_at
    age = max(0, int((now - oldest).total_seconds())) if oldest is not None else None
    return counts, age


def read_operations(db: Session, gate: DispatchGate) -> OperationsView:
    now = database_now(db)
    active = db.execute(active_jobs_query()).all()
    capped = len(active) > JOB_READ_LIMIT
    counts, age = ({}, None) if capped else summarize_jobs(active, now)
    dispatch = gate.statuses(JOB_KINDS)
    capacity = db.execute(capacity_runs_query()).mappings().all()
    quota = db.execute(quota_runs_query()).mappings().all()
    return OperationsView(
        assessed_at=now,
        jobs=[
            JobKindStatus(
                kind=kind,
                counts=counts.get(kind),
                dispatch="allowed" if dispatch[kind][0] else "paused",
                dispatch_reason=dispatch[kind][1],
            )
            for kind in JOB_KINDS
        ],
        jobs_capped=capped,
        job_read_limit=JOB_READ_LIMIT,
        oldest_runnable_age_seconds=age,
        capacity_deferred=[BlockedRun.model_validate(row) for row in capacity[:RUN_SAMPLE_LIMIT]],
        capacity_deferred_has_more=len(capacity) > RUN_SAMPLE_LIMIT,
        quota_waiting=[QuotaWaitingRun.model_validate(row) for row in quota[:RUN_SAMPLE_LIMIT]],
        quota_waiting_has_more=len(quota) > RUN_SAMPLE_LIMIT,
        run_sample_limit=RUN_SAMPLE_LIMIT,
    )
