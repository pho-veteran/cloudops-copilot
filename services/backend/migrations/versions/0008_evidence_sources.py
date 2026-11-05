"""Versioned source evidence and independent retention pins."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_evidence_sources"
down_revision = "0007_alert_intake"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "investigation_runs", sa.Column("pack_coverage", postgresql.JSONB(), nullable=True)
    )
    op.drop_constraint("evidence_run_id_key", "evidence", type_="unique")
    op.alter_column("evidence", "source", new_column_name="source_key")
    for name, typ in [
        ("source_kind", sa.String(16)),
        ("catalog_version", sa.String(32)),
        ("payload_kind", sa.String(16)),
        ("payload_version", sa.Integer()),
        ("requested_window", postgresql.JSONB()),
        ("time_relation", sa.String(32)),
        ("coverage", postgresql.JSONB()),
        ("query_metadata", postgresql.JSONB()),
        ("redaction_policy_version", sa.String(32)),
        ("expires_at", sa.DateTime(timezone=True)),
    ]:
        op.add_column("evidence", sa.Column(name, typ, nullable=True))
    op.execute("""UPDATE evidence SET source_key='development_fixture', source_kind='fixture',
        catalog_version='legacy_v1', payload_kind='fixture', payload_version=1,
        time_relation='not_captured', coverage='{"status":"unavailable","reason_codes":["not_captured"]}'::jsonb,
        redaction_policy_version='legacy_v1', expires_at=collected_at + interval '30 days'""")
    for name in (
        "source_kind",
        "catalog_version",
        "payload_kind",
        "payload_version",
        "time_relation",
        "coverage",
        "redaction_policy_version",
    ):
        op.alter_column("evidence", name, nullable=False)
    for name in ("observed_at", "sha256", "payload"):
        op.alter_column("evidence", name, nullable=True)
    op.create_unique_constraint(
        "evidence_source_identity", "evidence", ["run_id", "source_key", "catalog_version"]
    )
    op.create_table(
        "evidence_pins",
        sa.Column("evidence_id", sa.Uuid(), sa.ForeignKey("evidence.id"), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), primary_key=True),
    )
    op.execute("""INSERT INTO evidence_pins (evidence_id, kind, owner_id)
        SELECT e.id, 'resolution', a.id FROM evidence e
        JOIN investigation_runs r ON r.id=e.run_id
        JOIN incident_activity a ON a.incident_id=r.incident_id
        WHERE a.kind='resolve' ON CONFLICT DO NOTHING""")
    op.execute("""INSERT INTO evidence_pins (evidence_id, kind, owner_id)
        SELECT e.id, 'latest', i.id FROM incidents i
        JOIN LATERAL (SELECT r.id FROM investigation_runs r JOIN evidence x ON x.run_id=r.id
          WHERE r.incident_id=i.id AND x.payload IS NOT NULL ORDER BY r.created_at DESC, r.id DESC LIMIT 1) latest ON true
        JOIN evidence e ON e.run_id=latest.id WHERE i.state <> 'resolved' AND e.payload IS NOT NULL
        ON CONFLICT DO NOTHING""")


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM evidence UNION ALL SELECT 1 FROM investigation_runs WHERE pack_coverage IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("Evidence cannot be safely downgraded; restore a backup.")
    op.drop_table("evidence_pins")
    op.drop_constraint("evidence_source_identity", "evidence", type_="unique")
    for name in ("observed_at", "sha256", "payload"):
        op.alter_column("evidence", name, nullable=False)
    for name in (
        "source_kind",
        "catalog_version",
        "payload_kind",
        "payload_version",
        "requested_window",
        "time_relation",
        "coverage",
        "query_metadata",
        "redaction_policy_version",
        "expires_at",
    ):
        op.drop_column("evidence", name)
    op.alter_column("evidence", "source_key", new_column_name="source")
    op.create_unique_constraint("evidence_run_id_key", "evidence", ["run_id"])
    op.drop_column("investigation_runs", "pack_coverage")
