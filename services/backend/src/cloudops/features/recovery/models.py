from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
