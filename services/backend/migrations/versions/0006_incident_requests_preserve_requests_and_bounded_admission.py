"""preserve investigation requests and bounded deferred admission

Additive: existing incidents, runs and admissions keep their IDs, human text and committed
idempotency hashes. Legacy runs record "not_captured" instead of a fabricated request window.
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_incident_requests"
down_revision = "0005_workload_observations"
branch_labels = None
depends_on = None

ORIGIN = (
    "(origin_kind = 'operator' AND {actor} IS NOT NULL AND origin_source_id IS NULL) OR "
    "(origin_kind = 'alertmanager' AND {actor} IS NULL AND origin_source_id IS NOT NULL)"
)
OPEN_RUN = "state IN ('queued', 'collecting', 'analyzing', 'waiting_for_quota')"


def origin_columns(table):
    op.add_column(
        table,
        sa.Column("origin_kind", sa.String(length=16), server_default="operator", nullable=False),
    )
    op.add_column(table, sa.Column("origin_source_id", sa.String(length=64), nullable=True))


def upgrade():
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            f"SELECT incident_id FROM investigation_runs WHERE {OPEN_RUN} "
            "GROUP BY incident_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "An incident has more than one queued or collecting investigation. Let the extra "
            "runs finish or fail, then repeat the migration; no data was changed."
        )

    origin_columns("incidents")
    op.alter_column("incidents", "created_by", existing_type=sa.Uuid(), nullable=True)
    op.add_column("incidents", sa.Column("first_seen_at", sa.DateTime(timezone=True)))
    op.add_column("incidents", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column("incidents", sa.Column("severity", sa.String(length=32), nullable=True))
    op.add_column(
        "incidents",
        sa.Column(
            "severity_provenance",
            sa.String(length=16),
            server_default="not_provided",
            nullable=False,
        ),
    )
    op.add_column(
        "incidents",
        sa.Column("alert_signal", sa.String(length=16), server_default="none", nullable=False),
    )
    op.create_check_constraint("incident_origin", "incidents", ORIGIN.format(actor="created_by"))

    origin_columns("investigation_runs")
    op.alter_column("investigation_runs", "requested_by", existing_type=sa.Uuid(), nullable=True)
    op.add_column("investigation_runs", sa.Column("question", sa.Text(), nullable=True))
    op.add_column(
        "investigation_runs",
        sa.Column(
            "question_source", sa.String(length=16), server_default="not_captured", nullable=False
        ),
    )
    op.add_column("investigation_runs", sa.Column("window_start", sa.DateTime(timezone=True)))
    op.add_column("investigation_runs", sa.Column("window_end", sa.DateTime(timezone=True)))
    op.add_column(
        "investigation_runs",
        sa.Column(
            "window_source", sa.String(length=24), server_default="not_captured", nullable=False
        ),
    )
    op.add_column("investigation_runs", sa.Column("queue_reason", sa.String(length=16)))
    op.add_column("investigation_runs", sa.Column("outcome_code", sa.String(length=32)))
    op.create_check_constraint(
        "run_origin", "investigation_runs", ORIGIN.format(actor="requested_by")
    )
    op.create_check_constraint(
        "run_window",
        "investigation_runs",
        "(window_start IS NULL AND window_end IS NULL) OR "
        "(window_start IS NOT NULL AND window_end IS NOT NULL AND window_start < window_end)",
    )
    op.create_check_constraint(
        "run_queue_reason",
        "investigation_runs",
        "queue_reason IS NULL OR (state = 'queued' AND queue_reason = 'capacity')",
    )
    op.create_index(
        "one_open_run_per_incident",
        "investigation_runs",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_RUN),
    )
    op.create_index(
        "ix_runs_deferred",
        "investigation_runs",
        ["created_at", "id"],
        postgresql_where=sa.text("state = 'queued' AND queue_reason = 'capacity'"),
    )
    op.add_column(
        "admissions",
        sa.Column("signature_version", sa.SmallInteger(), server_default="1", nullable=False),
    )


def downgrade():
    connection = op.get_bind()
    blocked = connection.execute(
        sa.text(
            "SELECT 1 FROM incidents WHERE origin_kind <> 'operator' "
            "UNION ALL SELECT 1 FROM investigation_runs "
            "WHERE origin_kind <> 'operator' OR state = 'cancelled' OR queue_reason IS NOT NULL "
            "UNION ALL SELECT 1 FROM jobs WHERE state = 'cancelled' LIMIT 1"
        )
    ).first()
    if blocked:
        raise RuntimeError(
            "Downgrade would discard automatic origins, deferred or cancelled investigations. "
            "Restore from a backup taken before 0006 instead; no data was changed."
        )
    op.drop_column("admissions", "signature_version")
    op.drop_index("ix_runs_deferred", table_name="investigation_runs")
    op.drop_index("one_open_run_per_incident", table_name="investigation_runs")
    for name in ("run_queue_reason", "run_window", "run_origin"):
        op.drop_constraint(name, "investigation_runs", type_="check")
    for column in (
        "outcome_code",
        "queue_reason",
        "window_source",
        "window_end",
        "window_start",
        "question_source",
        "question",
        "origin_source_id",
        "origin_kind",
    ):
        op.drop_column("investigation_runs", column)
    op.alter_column("investigation_runs", "requested_by", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("incident_origin", "incidents", type_="check")
    for column in (
        "alert_signal",
        "severity_provenance",
        "severity",
        "last_seen_at",
        "first_seen_at",
        "origin_source_id",
        "origin_kind",
    ):
        op.drop_column("incidents", column)
    op.alter_column("incidents", "created_by", existing_type=sa.Uuid(), nullable=False)
