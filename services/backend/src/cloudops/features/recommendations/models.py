from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class RecommendationReport(Base):
    __tablename__ = "recommendation_reports"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    requested_hours: Mapped[int] = mapped_column(default=6)
    tool_version: Mapped[str] = mapped_column(String(64), default="krr-v1.31.1")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(String(512), default="Waiting for the bounded KRR worker.")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
