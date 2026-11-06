from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
        # A feature-declared boundary: E06 analysis reports whether a provider call is in flight.
        CheckConstraint(
            "provider_call IS NULL OR (provider_call IN ('none', 'in_flight') "
            "AND state IN ('analyzing', 'waiting_for_quota'))",
            name="run_provider_call",
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
    pack_coverage: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    provider_call: Mapped[str | None] = mapped_column(String(16))


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "source_key", "catalog_version", name="evidence_source_identity"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"))
    workload_uid: Mapped[str] = mapped_column(String(100))
    source_key: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(16))
    catalog_version: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[int] = mapped_column(default=2)
    payload_kind: Mapped[str] = mapped_column(String(16))
    payload_version: Mapped[int] = mapped_column(default=1)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_window: Mapped[dict | None] = mapped_column(JSONB)
    time_relation: Mapped[str] = mapped_column(String(32))
    coverage: Mapped[dict] = mapped_column(JSONB)
    query_metadata: Mapped[dict | None] = mapped_column(JSONB)
    sha256: Mapped[str | None] = mapped_column(String(64))
    redaction_policy_version: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), deferred=True)


class EvidencePin(Base):
    __tablename__ = "evidence_pins"
    evidence_id: Mapped[UUID] = mapped_column(ForeignKey("evidence.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
