from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, and_, func, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from cloudops.shared.config import settings
from cloudops.shared.database import Base, database_now, sessions


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("kind", "subject_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[UUID] = mapped_column()
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error: Mapped[str | None] = mapped_column(String(256))


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column()
    lease_token: Mapped[UUID] = mapped_column(unique=True)
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(256))


# These mechanics own leases only. Entrypoints inject feature transition policies.


class JobHandler(Protocol):
    def started(self, db: Session, subject_id: UUID): ...
    def failed(self, db: Session, subject_id: UUID, reason: str, terminal: bool): ...
    def publish(self, db: Session, subject_id: UUID, result: Any): ...


# Optional handler method: ``eligible(db, subject_id) -> bool``. It runs inside the claim
# transaction before a new attempt exists; False retires the job without an attempt.


def retire_queued(db: Session, kind: str, subject_id: UUID, reason: str) -> bool:
    """Retire an unclaimed job. A job locked by a claimer is skipped, never waited for."""
    job = db.scalar(
        select(Job)
        .where(Job.kind == kind, Job.subject_id == subject_id, Job.state == "queued")
        .with_for_update(skip_locked=True)
    )
    if not job:
        return False
    job.state, job.error = "cancelled", reason[:256]
    return True


@dataclass(frozen=True)
class Claim:
    id: UUID
    kind: str
    subject_id: UUID
    token: UUID
    attempt: int
    max_attempts: int


def claim_next(handlers: dict[str, JobHandler]) -> Claim | None:
    with sessions()() as db, db.begin():
        now = database_now(db)
        while True:
            job = db.scalar(
                select(Job)
                .where(
                    Job.kind.in_(handlers),
                    or_(
                        Job.state == "queued",
                        and_(Job.state == "running", Job.lease_until <= now),
                    ),
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return None
            handler = handlers[job.kind]
            if job.state == "running":
                attempt = db.scalar(
                    select(JobAttempt).where(JobAttempt.lease_token == job.lease_token)
                )
                attempt.state, attempt.finished_at, attempt.error = (
                    "expired",
                    now,
                    "Worker lease expired; collection may be retried.",
                )
            if job.attempts >= job.max_attempts:
                job.state, job.error = (
                    "failed",
                    "Collection attempt budget exhausted after worker lease expiry.",
                )
                job.lease_token, job.lease_until = None, None
                handler.failed(db, job.subject_id, job.error, True)
                return None
            eligible = getattr(handler, "eligible", None)
            if eligible is None or eligible(db, job.subject_id):
                break
            job.state, job.error = "cancelled", "Retired by its feature before another attempt."
            job.lease_token, job.lease_until = None, None
            db.flush()
        job.attempts += 1
        job.state, job.lease_token = "running", uuid4()
        job.lease_until = now + timedelta(seconds=settings().lease_seconds)
        db.add(
            JobAttempt(
                job_id=job.id,
                number=job.attempts,
                lease_token=job.lease_token,
                state="running",
                started_at=now,
            )
        )
        handler.started(db, job.subject_id)
        return Claim(
            job.id, job.kind, job.subject_id, job.lease_token, job.attempts, job.max_attempts
        )


def owned(db: Session, claim: Claim) -> Job | None:
    job = db.scalar(select(Job).where(Job.id == claim.id).with_for_update())
    if not job or job.state != "running" or job.lease_token != claim.token:
        return None
    if job.lease_until <= database_now(db):
        return None
    return job


def renew(claim: Claim) -> bool:
    with sessions()() as db, db.begin():
        job = owned(db, claim)
        if not job:
            return False
        job.lease_until = database_now(db) + timedelta(seconds=settings().lease_seconds)
        return True


def finish(claim: Claim, handler: JobHandler, result: Any = None, error: str | None = None) -> bool:
    with sessions()() as db, db.begin():
        job = owned(db, claim)
        if not job:
            return False
        now = database_now(db)
        attempt = db.scalar(select(JobAttempt).where(JobAttempt.lease_token == claim.token))
        attempt.finished_at = now
        if error:
            terminal = job.attempts >= job.max_attempts
            job.state, job.error = ("failed" if terminal else "queued"), error
            attempt.state, attempt.error = "failed", error
            handler.failed(db, job.subject_id, error, terminal)
        else:
            # Feature output and completion are one commit; rollback leaves the lease recoverable.
            handler.publish(db, job.subject_id, result)
            job.state, job.error, attempt.state = "completed", None, "succeeded"
        job.lease_token, job.lease_until = None, None
        return True
