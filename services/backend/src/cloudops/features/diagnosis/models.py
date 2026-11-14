"""Durable diagnosis records. Private context is never a response DTO."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class Analysis(Base):
    __tablename__ = "diagnosis_analyses"
    __table_args__ = (
        UniqueConstraint("run_id", "purpose"),
        CheckConstraint("purpose IN ('operational', 'evaluation')"),
        CheckConstraint("calls >= 0 AND calls <= 3 AND repairs >= 0 AND repairs <= 1"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16), default="operational")
    state: Mapped[str] = mapped_column(String(32), default="pending")
    phase: Mapped[str] = mapped_column(String(16), default="initial")
    reason: Mapped[str | None] = mapped_column(String(64))
    identity: Mapped[dict] = mapped_column(JSONB)
    dispatch_epoch: Mapped[dict] = mapped_column(JSONB)
    calls: Mapped[int] = mapped_column(default=0)
    repairs: Mapped[int] = mapped_column(default=0)
    feedback: Mapped[str | None] = mapped_column(String(4096))
    wait_mode: Mapped[str | None] = mapped_column(String(32))
    earliest_retry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisContext(Base):
    __tablename__ = "diagnosis_contexts"
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_analyses.id"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[dict] = mapped_column(JSONB)


class AnalysisCall(Base):
    __tablename__ = "diagnosis_calls"
    __table_args__ = (UniqueConstraint("analysis_id", "number"), UniqueConstraint("lease_token"))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_analyses.id"))
    number: Mapped[int] = mapped_column()
    lease_token: Mapped[UUID] = mapped_column()
    phase: Mapped[str] = mapped_column(String(16))
    request_digest: Mapped[str] = mapped_column(String(64))
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict | None] = mapped_column(JSONB)


class AnalysisResult(Base):
    __tablename__ = "diagnosis_results"
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_analyses.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class ModelCheck(Base):
    __tablename__ = "diagnosis_checks"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[UUID] = mapped_column(unique=True)
    identity: Mapped[dict] = mapped_column(JSONB)
    dispatch_epoch: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict | None] = mapped_column(JSONB)


class ResumeReceipt(Base):
    __tablename__ = "diagnosis_resumes"
    idempotency_key: Mapped[UUID] = mapped_column(primary_key=True)
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_analyses.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
