from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base

EPISODE_OUTCOMES = ("accepted", "rejected", "ignored")
RECEIPT_OUTCOMES = (
    "accepted",
    "deferred",
    "duplicate",
    "resolved_signal",
    "ignored",
    "rejected",
    "retryable",
)


def one_of(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AlertEpisode(Base):
    """One source/fingerprint/startsAt firing episode: consumed admission or terminal marker."""

    __tablename__ = "alert_episodes"
    __table_args__ = (
        UniqueConstraint("source_id", "fingerprint", "starts_at_key", name="alert_episode_key"),
        CheckConstraint(one_of("outcome", EPISODE_OUTCOMES), name="episode_outcome"),
        CheckConstraint(one_of("signal", ("firing", "resolved")), name="episode_signal"),
        CheckConstraint(
            "(outcome = 'accepted' AND workload_id IS NOT NULL AND incident_id IS NOT NULL "
            "AND run_id IS NOT NULL) OR "
            "(outcome <> 'accepted' AND incident_id IS NULL AND run_id IS NULL)",
            name="episode_binding",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    starts_at_key: Mapped[str] = mapped_column(String(30))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(32))
    workload_id: Mapped[UUID | None] = mapped_column(ForeignKey("workloads.id"))
    workload_uid: Mapped[str | None] = mapped_column(String(100))
    incident_id: Mapped[UUID | None] = mapped_column(ForeignKey("incidents.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("investigation_runs.id"))
    signal: Mapped[str] = mapped_column(String(16))
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertReceipt(Base):
    """Bounded, sanitized per-alert outcome history for authenticated deliveries."""

    __tablename__ = "alert_intake_receipts"
    __table_args__ = (
        CheckConstraint(one_of("outcome", RECEIPT_OUTCOMES), name="receipt_outcome"),
        CheckConstraint(one_of("alert_status", ("firing", "resolved")), name="receipt_status"),
        Index("ix_alert_receipts_received", "received_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[str] = mapped_column(String(64))
    alert_status: Mapped[str] = mapped_column(String(16))
    fingerprint: Mapped[str] = mapped_column(String(64))
    starts_at_key: Mapped[str] = mapped_column(String(30))
    alertname: Mapped[str | None] = mapped_column(String(100))
    namespace: Mapped[str | None] = mapped_column(String(100))
    target_kind: Mapped[str | None] = mapped_column(String(16))
    target_name: Mapped[str | None] = mapped_column(String(253))
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(32))
    # Plain identifiers, not foreign keys, so bounded pruning stays independent.
    episode_id: Mapped[UUID | None] = mapped_column()
    incident_id: Mapped[UUID | None] = mapped_column()
    run_id: Mapped[UUID | None] = mapped_column()
    workload_id: Mapped[UUID | None] = mapped_column()
