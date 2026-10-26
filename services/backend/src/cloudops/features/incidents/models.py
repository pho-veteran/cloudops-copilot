from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from cloudops.shared.database import Base


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="open")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Admission(Base):
    __tablename__ = "admissions"
    __table_args__ = (UniqueConstraint("actor_id", "key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"))


class IncidentActivity(Base):
    __tablename__ = "incident_activity"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
