from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from cloudops.shared.database import Base

# A human actor and a trusted automatic source are mutually exclusive request origins.
ORIGIN_CHECK = (
    "(origin_kind = 'operator' AND {actor} IS NOT NULL AND origin_source_id IS NULL) OR "
    "(origin_kind = 'alertmanager' AND {actor} IS NULL AND origin_source_id IS NOT NULL)"
)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(ORIGIN_CHECK.format(actor="created_by"), name="incident_origin"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="open")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    origin_kind: Mapped[str] = mapped_column(
        String(16), default="operator", server_default="operator"
    )
    origin_source_id: Mapped[str | None] = mapped_column(String(64))
    # Alert intake records these; manual and legacy rows truthfully keep them unknown.
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str | None] = mapped_column(String(32))
    severity_provenance: Mapped[str] = mapped_column(
        String(16), default="not_provided", server_default="not_provided"
    )
    alert_signal: Mapped[str] = mapped_column(String(16), default="none", server_default="none")


class Admission(Base):
    __tablename__ = "admissions"
    __table_args__ = (UniqueConstraint("actor_id", "key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"))
    # 1: committed pre-request-window signature; 2: canonical request signature.
    signature_version: Mapped[int] = mapped_column(SmallInteger, default=2, server_default="1")


class IncidentActivity(Base):
    __tablename__ = "incident_activity"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
