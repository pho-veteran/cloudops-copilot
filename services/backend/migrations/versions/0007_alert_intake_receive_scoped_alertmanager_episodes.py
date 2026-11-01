"""receive scoped Alertmanager episodes and intake receipts

Additive: two new tables. Existing incidents, runs, admissions and activity are unchanged.
Episodes are the durable deduplication identity; receipts are bounded sanitized history.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_alert_intake"
down_revision = "0006_incident_requests"
branch_labels = None
depends_on = None

EPISODE_OUTCOMES = "outcome IN ('accepted', 'rejected', 'ignored')"
RECEIPT_OUTCOMES = (
    "outcome IN ('accepted', 'deferred', 'duplicate', 'resolved_signal', 'ignored', "
    "'rejected', 'retryable')"
)
SIGNAL = "{column} IN ('firing', 'resolved')"
BINDING = (
    "(outcome = 'accepted' AND workload_id IS NOT NULL AND incident_id IS NOT NULL "
    "AND run_id IS NOT NULL) OR "
    "(outcome <> 'accepted' AND incident_id IS NULL AND run_id IS NULL)"
)


def upgrade():
    op.create_table(
        "alert_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("starts_at_key", sa.String(length=30), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("workload_id", sa.Uuid(), nullable=True),
        sa.Column("workload_uid", sa.String(length=100), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("signal", sa.String(length=16), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workload_id"], ["workloads.id"]),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["investigation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "fingerprint", "starts_at_key", name="alert_episode_key"),
        sa.CheckConstraint(EPISODE_OUTCOMES, name="episode_outcome"),
        sa.CheckConstraint(SIGNAL.format(column="signal"), name="episode_signal"),
        sa.CheckConstraint(BINDING, name="episode_binding"),
    )
    op.create_table(
        "alert_intake_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("alert_status", sa.String(length=16), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("starts_at_key", sa.String(length=30), nullable=False),
        sa.Column("alertname", sa.String(length=100), nullable=True),
        sa.Column("namespace", sa.String(length=100), nullable=True),
        sa.Column("target_kind", sa.String(length=16), nullable=True),
        sa.Column("target_name", sa.String(length=253), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("episode_id", sa.Uuid(), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("workload_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(RECEIPT_OUTCOMES, name="receipt_outcome"),
        sa.CheckConstraint(SIGNAL.format(column="alert_status"), name="receipt_status"),
    )
    op.create_index("ix_alert_receipts_received", "alert_intake_receipts", ["received_at", "id"])


def downgrade():
    connection = op.get_bind()
    blocked = connection.execute(
        sa.text(
            "SELECT 1 FROM alert_episodes UNION ALL SELECT 1 FROM alert_intake_receipts LIMIT 1"
        )
    ).first()
    if blocked:
        raise RuntimeError(
            "Downgrade would discard alert episode bindings and intake receipts, which would let "
            "old deliveries create new work. Restore from a backup taken before 0007 instead; "
            "no data was changed."
        )
    op.drop_index("ix_alert_receipts_received", table_name="alert_intake_receipts")
    op.drop_table("alert_intake_receipts")
    op.drop_table("alert_episodes")
