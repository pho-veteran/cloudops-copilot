from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
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


class BaselineCandidate(Base):
    __tablename__ = "baseline_candidates"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkloadObservationState(Base):
    """One bounded scheduler/lease projection per configured workload identity."""

    __tablename__ = "workload_observation_states"
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), primary_key=True)
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_state: Mapped[str | None] = mapped_column(String(16))
    last_attempt_reason: Mapped[str | None] = mapped_column(String(64))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkloadObservation(Base):
    """Immutable successful observation history; bounded by retention policy."""

    __tablename__ = "workload_observations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    deployment_uid: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WorkloadObservationAttempt(Base):
    """Bounded success/failure attempt record, independent of success history."""

    __tablename__ = "workload_observation_attempts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


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
