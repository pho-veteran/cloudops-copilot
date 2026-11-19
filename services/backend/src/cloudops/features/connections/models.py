from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class ConnectionCheck(Base):
    __tablename__ = "connection_checks"
    __table_args__ = (
        Index("ix_connection_checks_history", "observed_at", "id"),
        Index("ix_connection_checks_attempt", "component", "attempted_at", "id"),
        Index(
            "ix_connection_checks_binding",
            "component",
            "binding_key",
            "status",
            "attempted_at",
            "id",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    component: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    idempotency_key: Mapped[UUID | None] = mapped_column(unique=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    binding_key: Mapped[str | None] = mapped_column(String(64))
    config_id: Mapped[str | None] = mapped_column(String(64))
    target: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    reason_code: Mapped[str | None] = mapped_column(String(32))
    metrics: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
