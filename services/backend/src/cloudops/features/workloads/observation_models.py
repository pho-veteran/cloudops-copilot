"""Feature-owned observation state; independent of product jobs and baselines."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class ObservationState(Base):
    __tablename__ = "workload_observation_state"
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), primary_key=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sequence: Mapped[int] = mapped_column(default=0)
    success_sequence: Mapped[int | None]
    failures: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str | None] = mapped_column(String(32))
    latest_outcome: Mapped[str | None] = mapped_column(String(16))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_reason: Mapped[str | None] = mapped_column(String(64))
    last_success_id: Mapped[UUID | None]
    attempt_binding: Mapped[str | None] = mapped_column(String(64))
    success_binding: Mapped[str | None] = mapped_column(String(64))
    failure_binding: Mapped[str | None] = mapped_column(String(64))
    bound_attempt_sequence: Mapped[int | None]
    bound_success_id: Mapped[UUID | None]
    bound_success_config_id: Mapped[str | None] = mapped_column(String(64))
    bound_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    partial_payload: Mapped[dict | None] = mapped_column(JSONB)


class WorkloadObservation(Base):
    __tablename__ = "workload_observations"
    __table_args__ = (Index("ix_observations_workload_window", "workload_id", "observed_at", "id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"))
    epoch: Mapped[int]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    comparison: Mapped[dict] = mapped_column(JSONB)


class ObservationAttempt(Base):
    __tablename__ = "workload_observation_attempts"
    __table_args__ = (
        Index("ix_observation_attempts_workload_time", "workload_id", "started_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"))
    sequence: Mapped[int]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(64))
