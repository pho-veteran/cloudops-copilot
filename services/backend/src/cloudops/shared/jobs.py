import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from cloudops.shared.config import settings
from cloudops.shared.database import Base, database_now, sessions
from cloudops.shared.dispatch_permit import DispatchGate


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
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


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
    # Evidence of the host permit that admitted this attempt; it never grants permission.
    permit_authority: Mapped[UUID | None] = mapped_column()
    permit_generation: Mapped[int | None] = mapped_column(BigInteger)


STAGE = re.compile(r"[a-z_]+(:[a-z0-9_.]+)?")


class JobCheckpoint(Base):
    """One committed, fenced stage of a job. Stage meaning belongs to the job's feature."""

    __tablename__ = "job_checkpoints"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="job_checkpoint_sequence"),
        CheckConstraint("stage ~ '^[a-z_]+(:[a-z0-9_.]+)?$'", name="job_checkpoint_stage"),
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(96), primary_key=True)
    sequence: Mapped[int] = mapped_column()
    attempt_number: Mapped[int] = mapped_column()
    lease_token: Mapped[UUID] = mapped_column()
    permit_authority: Mapped[UUID] = mapped_column()
    permit_generation: Mapped[int] = mapped_column(BigInteger)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    carry: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


# These mechanics own leases, checkpoints and permit fences only. Entrypoints inject feature
# transition policies and the job-kind to permit-class mapping through ``DispatchGate``.
# Lock order: host shared permit lock -> one short PostgreSQL transaction. Never the reverse.


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
    permit_authority: UUID | None = None
    permit_generation: int | None = None


def claim_next(handlers: dict[str, JobHandler], gate: DispatchGate) -> Claim | None:
    """Claim one permitted job. Denied kinds are not touched and consume no attempt."""
    with gate.admitted() as admission, sessions()() as db, db.begin():
        kinds = [kind for kind in handlers if kind in admission.kinds]
        if not kinds:
            return None
        now = database_now(db)
        while True:
            job = db.scalar(
                select(Job)
                .where(
                    Job.kind.in_(kinds),
                    or_(Job.due_at.is_(None), Job.due_at <= now),
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
        permit = admission.permit
        db.add(
            JobAttempt(
                job_id=job.id,
                number=job.attempts,
                lease_token=job.lease_token,
                state="running",
                started_at=now,
                permit_authority=permit.authority_id,
                permit_generation=permit.generation,
            )
        )
        handler.started(db, job.subject_id)
        return Claim(
            job.id,
            job.kind,
            job.subject_id,
            job.lease_token,
            job.attempts,
            job.max_attempts,
            permit.authority_id,
            permit.generation,
        )


def owned(db: Session, claim: Claim) -> Job | None:
    job = db.scalar(select(Job).where(Job.id == claim.id).with_for_update())
    if not job or job.state != "running" or job.lease_token != claim.token:
        return None
    if job.lease_until <= database_now(db):
        return None
    return job


def _fenced(db: Session, claim: Claim, admission) -> tuple[Job, JobAttempt] | None:
    """Current token, unexpired lease and the same permit generation that admitted the claim."""
    job = owned(db, claim)
    if not job:
        return None
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.lease_token == claim.token))
    if not admission.permits(claim.kind, attempt.permit_authority, attempt.permit_generation):
        return None
    return job, attempt


def start_call(claim: Claim, gate: DispatchGate) -> bool:
    """Admit one external call under the host lock and the live attempt fence.

    This short transaction is the call-start linearization point, not network quiescence:
    recovery ordered after it may leave an admitted call in flight. Neither lock spans I/O,
    and checkpoint/publication still require the current permit generation.
    """
    with gate.admitted() as admission, sessions()() as db, db.begin():
        return _fenced(db, claim, admission) is not None


def renew(claim: Claim) -> bool:
    with sessions()() as db, db.begin():
        job = owned(db, claim)
        if not job:
            return False
        job.lease_until = database_now(db) + timedelta(seconds=settings().lease_seconds)
        return True


def committed_stages(claim: Claim) -> set[str]:
    with sessions()() as db:
        return set(db.scalars(select(JobCheckpoint.stage).where(JobCheckpoint.job_id == claim.id)))


def checkpoint(
    claim: Claim,
    stage: str,
    gate: DispatchGate,
    write: Callable[[Session, dict | None], dict | None],
) -> bool:
    """Commit one stage atomically with its feature output under the current lease and permit.

    ``write`` receives the previous stage's carry and returns the next. A committed stage is
    never rewritten; False means the claim is obsolete and nothing was written.
    """
    if len(stage) > 96 or not STAGE.fullmatch(stage):
        raise ValueError("Invalid checkpoint stage.")
    with gate.admitted() as admission, sessions()() as db, db.begin():
        fenced = _fenced(db, claim, admission)
        if not fenced:
            return False
        job, attempt = fenced
        if db.get(JobCheckpoint, (job.id, stage)):
            return True
        previous = db.scalar(
            select(JobCheckpoint)
            .where(JobCheckpoint.job_id == job.id)
            .order_by(JobCheckpoint.sequence.desc())
            .limit(1)
        )
        carry = write(db, previous.carry if previous else None)
        if carry is not None and len(json.dumps(carry, separators=(",", ":")).encode()) > 16384:
            raise ValueError("Checkpoint carry exceeds its bound.")
        db.add(
            JobCheckpoint(
                job_id=job.id,
                stage=stage,
                sequence=(previous.sequence + 1) if previous else 1,
                attempt_number=attempt.number,
                lease_token=claim.token,
                permit_authority=attempt.permit_authority,
                permit_generation=attempt.permit_generation,
                committed_at=database_now(db),
                carry=carry,
            )
        )
        return True


def defer_owned(db: Session, claim: Claim, due_at: datetime | None) -> None:
    """Release a fenced claim in its caller's transaction; None holds it until resumed.

    The caller must already have obtained ``_fenced`` under its dispatch admission.
    Feature outcome, attempt completion and deferral must commit together.
    """
    job = owned(db, claim)
    if job is None:
        raise RuntimeError("Cannot defer an obsolete claim.")
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.lease_token == claim.token))
    attempt.state, attempt.finished_at = "succeeded", database_now(db)
    job.state = "held" if due_at is None else "queued"
    job.due_at = due_at
    job.lease_token, job.lease_until = None, None


def finish(
    claim: Claim,
    handler: JobHandler,
    result: Any = None,
    error: str | None = None,
    *,
    gate: DispatchGate,
) -> bool:
    with gate.admitted() as admission, sessions()() as db, db.begin():
        fenced = _fenced(db, claim, admission)
        if not fenced:
            return False
        job, attempt = fenced
        now = database_now(db)
        attempt.finished_at = now
        if error:
            terminal = job.attempts >= job.max_attempts
            job.state, job.error = ("failed" if terminal else "queued"), error
            attempt.state, attempt.error = "failed", error
            handler.failed(db, job.subject_id, error, terminal)
        else:
            # Feature output and completion are one commit; rollback leaves the lease recoverable.
            handler.publish(db, job.subject_id, result)
            stage = getattr(result, "checkpoint_stage", None)
            if stage:
                if len(stage) > 96 or not STAGE.fullmatch(stage):
                    raise ValueError("Invalid checkpoint stage.")
                last = db.scalar(
                    select(func.max(JobCheckpoint.sequence)).where(JobCheckpoint.job_id == job.id)
                )
                db.add(
                    JobCheckpoint(
                        job_id=job.id,
                        stage=stage,
                        sequence=(last or 0) + 1,
                        attempt_number=attempt.number,
                        lease_token=claim.token,
                        permit_authority=attempt.permit_authority,
                        permit_generation=attempt.permit_generation,
                        committed_at=now,
                        carry=None,
                    )
                )
            job.state, job.error, attempt.state = "completed", None, "succeeded"
        job.lease_token, job.lease_until = None, None
        return True
