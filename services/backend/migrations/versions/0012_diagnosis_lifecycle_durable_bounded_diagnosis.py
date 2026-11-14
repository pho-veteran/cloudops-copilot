"""durable bounded diagnosis"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_diagnosis_lifecycle"
down_revision = "0011_knowledge_selections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "diagnosis_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dispatch_epoch", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "diagnosis_analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dispatch_epoch", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("repairs", sa.Integer(), nullable=False),
        sa.Column("feedback", sa.String(length=4096), nullable=True),
        sa.Column("wait_mode", sa.String(length=32), nullable=True),
        sa.Column("earliest_retry", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('operational', 'evaluation')"),
        sa.CheckConstraint("calls >= 0 AND calls <= 3 AND repairs >= 0 AND repairs <= 1"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "purpose"),
    )
    op.create_index(
        op.f("ix_diagnosis_analyses_run_id"), "diagnosis_analyses", ["run_id"], unique=False
    )
    op.create_table(
        "diagnosis_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["diagnosis_analyses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", "number"),
        sa.UniqueConstraint("lease_token"),
    )
    op.create_table(
        "diagnosis_contexts",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["diagnosis_analyses.id"],
        ),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_table(
        "diagnosis_results",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["diagnosis_analyses.id"],
        ),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_table(
        "diagnosis_resumes",
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["diagnosis_analyses.id"],
        ),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.add_column("jobs", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_jobs_due_at"), "jobs", ["due_at"], unique=False)


def downgrade():
    for table in ("diagnosis_analyses", "diagnosis_checks"):
        if op.get_bind().execute(sa.text("SELECT 1 FROM " + table + " LIMIT 1")).first():
            raise RuntimeError("Diagnosis history cannot be discarded; restore a backup.")
    op.drop_index(op.f("ix_jobs_due_at"), table_name="jobs")
    op.drop_column("jobs", "due_at")
    op.drop_table("diagnosis_resumes")
    op.drop_table("diagnosis_results")
    op.drop_table("diagnosis_contexts")
    op.drop_table("diagnosis_calls")
    op.drop_index(op.f("ix_diagnosis_analyses_run_id"), table_name="diagnosis_analyses")
    op.drop_table("diagnosis_analyses")
    op.drop_table("diagnosis_checks")
