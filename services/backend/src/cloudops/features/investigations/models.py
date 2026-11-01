from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.features.incidents.models import ORIGIN_CHECK
from cloudops.features.investigations.states import NONTERMINAL_RUN
from cloudops.shared.database import Base

OPEN_RUN = "state IN (" + ", ".join(f"'{state}'" for state in NONTERMINAL_RUN) + ")"


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint(ORIGIN_CHECK.format(actor="requested_by"), name="run_origin"),
        CheckConstraint(
            "(window_start IS NULL AND window_end IS NULL) OR "
            "(window_start IS NOT NULL AND window_end IS NOT NULL AND window_start < window_end)",
            name="run_window",
        ),
        CheckConstraint(
            "queue_reason IS NULL OR (state = 'queued' AND queue_reason = 'capacity')",
            name="run_queue_reason",
        ),
        Index(
            "one_open_run_per_incident", "incident_id", unique=True, postgresql_where=text(OPEN_RUN)
        ),
        Index(
            "ix_runs_deferred",
            "created_at",
            "id",
            postgresql_where=text("state = 'queued' AND queue_reason = 'capacity'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    reason: Mapped[str] = mapped_column(String(256), default="Waiting for a collection worker.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    origin_kind: Mapped[str] = mapped_column(
        String(16), default="operator", server_default="operator"
    )
    origin_source_id: Mapped[str | None] = mapped_column(String(64))
    # The immutable request. Legacy rows keep "not_captured" rather than an invented window.
    question: Mapped[str | None] = mapped_column(Text)
    question_source: Mapped[str] = mapped_column(
        String(16), default="not_captured", server_default="not_captured"
    )
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_source: Mapped[str] = mapped_column(
        String(24), default="not_captured", server_default="not_captured"
    )
    queue_reason: Mapped[str | None] = mapped_column(String(16))
    outcome_code: Mapped[str | None] = mapped_column(String(32))


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
