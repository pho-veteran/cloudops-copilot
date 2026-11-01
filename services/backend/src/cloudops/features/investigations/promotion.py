"""Bounded oldest-first promotion of deferred investigations on ordinary worker cycles."""

from sqlalchemy import func, select

from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.service import (
    DISPATCH_REASON,
    dispatch_blocker,
    terminalize,
)
from cloudops.features.investigations.states import COLLECTION_JOB
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.jobs import Job


def jobs_full(db) -> bool:
    active = db.scalar(
        select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))
    )
    return active >= settings().queue_limit


def deferred_count(db) -> int:
    return db.scalar(
        select(func.count())
        .select_from(InvestigationRun)
        .where(InvestigationRun.state == "queued", InvestigationRun.queue_reason == "capacity")
    )


def promote_deferred(limit: int = 5) -> int:
    """Promote at most ``limit`` Runs; one short transaction each, no network work."""
    from cloudops.features.recovery.service import invalidate

    promoted = 0
    for _ in range(limit):
        with sessions()() as db:
            # Cheap unlocked probe so idle worker cycles never take the admission lock.
            if not db.scalar(
                select(InvestigationRun.id)
                .where(
                    InvestigationRun.state == "queued",
                    InvestigationRun.queue_reason == "capacity",
                )
                .limit(1)
            ):
                return promoted
        with sessions()() as db, db.begin():
            lock_admission(db)
            run = db.scalar(
                select(InvestigationRun)
                .where(
                    InvestigationRun.state == "queued",
                    InvestigationRun.queue_reason == "capacity",
                )
                .order_by(InvestigationRun.created_at, InvestigationRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is None:
                return promoted
            code = dispatch_blocker(db, run)
            if code:
                terminalize(db, run, code)
                continue
            if jobs_full(db):
                return promoted
            # A draft proposed while this Run waited predates the evidence it will collect.
            invalidate(db, run.incident_id)
            db.add(Job(kind=COLLECTION_JOB, subject_id=run.id))
            run.queue_reason, run.reason = None, DISPATCH_REASON
            promoted += 1
    return promoted
