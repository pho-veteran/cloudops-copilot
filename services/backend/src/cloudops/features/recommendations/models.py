from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class RecommendationReport(Base):
    __tablename__ = "recommendation_reports"
    __table_args__ = (
        Index("ix_recommendation_reports_history", "created_at", "id"),
        Index(
            "ix_recommendation_reports_qualified_history",
            "finished_at",
            "id",
            postgresql_where=text(
                "schema_version = 2 AND state = 'available' AND finished_at IS NOT NULL"
            ),
        ),
    )
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
    schema_version: Mapped[int] = mapped_column(default=1, server_default="1")
    input_identity: Mapped[dict | None] = mapped_column(JSONB)
    profile: Mapped[dict | None] = mapped_column(JSONB)
    result_v2: Mapped[dict | None] = mapped_column(JSONB)
    export_text: Mapped[str | None] = mapped_column(Text)
    export_sha256: Mapped[str | None] = mapped_column(String(64))
    migration_disposition: Mapped[str | None] = mapped_column(String(64))
