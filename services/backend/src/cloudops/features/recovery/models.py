from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class ActionPlan(Base):
    __tablename__ = "action_plans"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    template: Mapped[str] = mapped_column(String(64), default="scale_deployment")
    origin: Mapped[str | None] = mapped_column(String(32))
    source_analysis_id: Mapped[UUID | None] = mapped_column(ForeignKey("diagnosis_analyses.id"))
    forward_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recovery_executions.id", use_alter=True), index=True
    )
    content: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    target_epoch: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="proposed")
    rationale: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanReview(Base):
    __tablename__ = "plan_reviews"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("action_plans.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    session_binding: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(default=False)


class Execution(Base):
    __tablename__ = "recovery_executions"
    __table_args__ = (
        CheckConstraint("reconcile_attempts >= 0", name="reconcile_attempts_nonnegative"),
        Index(
            "one_recovery_per_target",
            "workload_id",
            unique=True,
            postgresql_where=text("holds_lock"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("action_plans.id"), unique=True)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    holds_lock: Mapped[bool] = mapped_column(default=True)
    maintenance_released: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    dispatch_request: Mapped[dict | None] = mapped_column(JSONB)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def outcome(self):
        from cloudops.features.recovery.outcomes import project

        return project(self)


class Verification(Base):
    __tablename__ = "recovery_verifications"
    __table_args__ = (
        CheckConstraint("next_slot >= 0 AND next_slot <= 11", name="verification_slot_bound"),
        CheckConstraint(
            "deadline IS NULL OR deadline > acknowledged_at", name="verification_window"
        ),
    )
    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("recovery_executions.id"), primary_key=True
    )
    profile: Mapped[dict] = mapped_column(JSONB)
    profile_digest: Mapped[str] = mapped_column(String(64))
    baseline: Mapped[dict | None] = mapped_column(JSONB)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_slot: Mapped[int] = mapped_column(default=0, server_default="0")
    samples: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RollbackWindow(Base):
    __tablename__ = "recovery_rollback_windows"
    __table_args__ = (CheckConstraint("deadline > eligible_from", name="rollback_window_positive"),)
    forward_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("recovery_executions.id"), primary_key=True
    )
    eligible_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_digest: Mapped[str] = mapped_column(String(64))
    inverse_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recovery_executions.id"), unique=True
    )
