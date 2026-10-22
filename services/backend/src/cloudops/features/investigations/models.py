from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    reason: Mapped[str] = mapped_column(String(256), default="Waiting for a collection worker.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"), unique=True)
    workload_uid: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(default=1)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
