"""add access consumers and recovery approvals"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_e01_consumers"
down_revision = "0002_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "baseline_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workload_id"],
            ["workloads.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_baseline_candidates_workload_id"),
        "baseline_candidates",
        ["workload_id"],
        unique=False,
    )
    op.create_table(
        "connection_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "recommendation_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("requested_hours", sa.Integer(), nullable=False),
        sa.Column("tool_version", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workload_id"],
            ["workloads.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recommendation_reports_workload_id"),
        "recommendation_reports",
        ["workload_id"],
        unique=False,
    )
    op.create_table(
        "action_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("target_epoch", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workload_id"],
            ["workloads.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_action_plans_incident_id"), "action_plans", ["incident_id"], unique=False
    )
    op.create_index(
        op.f("ix_action_plans_workload_id"), "action_plans", ["workload_id"], unique=False
    )
    op.create_table(
        "baselines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["baseline_candidates.id"],
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workload_id"],
            ["workloads.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id"),
    )
    op.create_index(op.f("ix_baselines_workload_id"), "baselines", ["workload_id"], unique=False)
    op.create_table(
        "incident_activity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_incident_activity_incident_id"), "incident_activity", ["incident_id"], unique=False
    )
    op.create_table(
        "plan_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("session_binding", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["action_plans.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_plan_reviews_plan_id"), "plan_reviews", ["plan_id"], unique=False)
    op.create_table(
        "recovery_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("holds_lock", sa.Boolean(), nullable=False),
        sa.Column("maintenance_released", sa.Boolean(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["action_plans.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workload_id"],
            ["workloads.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id"),
    )
    op.create_index(
        op.f("ix_recovery_executions_workload_id"),
        "recovery_executions",
        ["workload_id"],
        unique=False,
    )
    op.create_index(
        "one_recovery_per_target",
        "recovery_executions",
        ["workload_id"],
        unique=True,
        postgresql_where=sa.text("holds_lock"),
    )
    op.add_column(
        "workloads",
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="development_fixture"
        ),
    )
    op.alter_column("workloads", "source", server_default=None)


def downgrade():
    op.drop_column("workloads", "source")
    op.drop_index(
        "one_recovery_per_target",
        table_name="recovery_executions",
        postgresql_where=sa.text("holds_lock"),
    )
    op.drop_index(op.f("ix_recovery_executions_workload_id"), table_name="recovery_executions")
    op.drop_table("recovery_executions")
    op.drop_index(op.f("ix_plan_reviews_plan_id"), table_name="plan_reviews")
    op.drop_table("plan_reviews")
    op.drop_index(op.f("ix_incident_activity_incident_id"), table_name="incident_activity")
    op.drop_table("incident_activity")
    op.drop_index(op.f("ix_baselines_workload_id"), table_name="baselines")
    op.drop_table("baselines")
    op.drop_index(op.f("ix_action_plans_workload_id"), table_name="action_plans")
    op.drop_index(op.f("ix_action_plans_incident_id"), table_name="action_plans")
    op.drop_table("action_plans")
    op.drop_index(
        op.f("ix_recommendation_reports_workload_id"), table_name="recommendation_reports"
    )
    op.drop_table("recommendation_reports")
    op.drop_table("connection_checks")
    op.drop_index(op.f("ix_baseline_candidates_workload_id"), table_name="baseline_candidates")
    op.drop_table("baseline_candidates")
