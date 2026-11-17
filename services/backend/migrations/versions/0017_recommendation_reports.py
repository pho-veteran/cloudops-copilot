"""Preserve cutover exports and retire legacy recommendation attempts."""

from datetime import datetime
import hashlib
import json
from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_recommendation_reports"
down_revision = "0016_recovery_inverse"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # The caller stops admission/dispatch; these locks exclude already-running publishers.
    connection.execute(
        sa.text("LOCK TABLE recommendation_reports, jobs, job_attempts IN ACCESS EXCLUSIVE MODE")
    )
    columns = [
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_identity", postgresql.JSONB(), nullable=True),
        sa.Column("profile", postgresql.JSONB(), nullable=True),
        sa.Column("result_v2", postgresql.JSONB(), nullable=True),
        sa.Column("export_text", sa.Text(), nullable=True),
        sa.Column("export_sha256", sa.String(64), nullable=True),
        sa.Column("migration_disposition", sa.String(64), nullable=True),
    ]
    for column in columns:
        op.add_column("recommendation_reports", column)
    fields = (
        "id",
        "workload_id",
        "state",
        "requested_hours",
        "tool_version",
        "result",
        "reason",
        "created_by",
        "created_at",
        "finished_at",
    )
    rows = connection.execute(
        sa.text("SELECT " + ",".join(fields) + " FROM recommendation_reports")
    ).mappings()
    for row in rows:
        value = {}
        for key in fields:
            item = row[key]
            if isinstance(item, UUID):
                item = str(item)
            elif isinstance(item, datetime):
                item = item.isoformat().replace("+00:00", "Z")
            value[key] = item
        frozen = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        connection.execute(
            sa.text(
                "UPDATE recommendation_reports SET export_text=:frozen, export_sha256=:digest, "
                "migration_disposition=:disposition WHERE id=:id"
            ),
            {
                "frozen": frozen,
                "digest": hashlib.sha256(frozen.encode()).hexdigest(),
                "disposition": "interrupted_at_cutover"
                if row["state"] in {"queued", "running"}
                else "legacy_cutover",
                "id": row["id"],
            },
        )
    connection.execute(
        sa.text(
            "UPDATE job_attempts SET state='expired', finished_at=clock_timestamp(), "
            "error='Legacy recommendation attempt retired at schema cutover.' "
            "WHERE state='running' AND job_id IN "
            "(SELECT id FROM jobs WHERE kind='recommendation_report_v1' AND state IN ('queued','running'))"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE jobs SET state='cancelled', lease_token=NULL, lease_until=NULL, "
            "error='Legacy recommendation job retired at schema cutover; create a new report.' "
            "WHERE kind='recommendation_report_v1' AND state IN ('queued','running')"
        )
    )
    op.create_index(
        "ix_recommendation_reports_history", "recommendation_reports", ["created_at", "id"]
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM recommendation_reports WHERE schema_version=2)")
    ):
        raise RuntimeError(
            "Versioned recommendation reports cannot be discarded; retain this schema."
        )
    op.drop_index("ix_recommendation_reports_history", table_name="recommendation_reports")
    for name in (
        "migration_disposition",
        "export_sha256",
        "export_text",
        "result_v2",
        "profile",
        "input_identity",
        "schema_version",
    ):
        op.drop_column("recommendation_reports", name)
