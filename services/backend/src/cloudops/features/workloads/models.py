from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class Workload(Base):
    __tablename__ = "workloads"
    __table_args__ = (UniqueConstraint("cluster", "namespace", "name", "uid"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cluster: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    uid: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)
    source: Mapped[str] = mapped_column(String(32), default="development_fixture")
    retirement_reason: Mapped[str | None] = mapped_column(String(32))
    observation_epoch: Mapped[int] = mapped_column(default=0, server_default="0")


class ScopeState(Base):
    __tablename__ = "workload_scope_state"
    __table_args__ = (CheckConstraint("id = 1", name="scope_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    latest_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="legacy")
    cluster_key: Mapped[str | None] = mapped_column(String(100))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    config_digest: Mapped[str | None] = mapped_column(String(64))


class ScopeTarget(Base):
    __tablename__ = "workload_scope_targets"
    __table_args__ = (UniqueConstraint("cluster", "namespace", "name"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cluster: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    configured: Mapped[bool] = mapped_column(default=True)
    workload_id: Mapped[UUID | None] = mapped_column(ForeignKey("workloads.id"))
    state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(32))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BaselineCandidate(Base):
    __tablename__ = "baseline_candidates"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Baseline(Base):
    __tablename__ = "baselines"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("baseline_candidates.id"), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
