"""add workload observation state, history and attempts"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_workload_observations"
down_revision = "0003_e01_consumers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workload_observation_states",
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_state", sa.String(length=16), nullable=True),
        sa.Column("last_attempt_reason", sa.String(length=64), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workload_id"], ["workloads.id"]),
        sa.PrimaryKeyConstraint("workload_id"),
    )
    op.create_index(
        op.f("ix_workload_observation_states_due_at"),
        "workload_observation_states",
        ["due_at"],
        unique=False,
    )
    op.create_table(
        "workload_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_uid", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workload_id"], ["workloads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workload_observations_workload_id"),
        "workload_observations",
        ["workload_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workload_observations_observed_at"),
        "workload_observations",
        ["observed_at"],
        unique=False,
    )
    op.create_table(
        "workload_observation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workload_id"], ["workloads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workload_observation_attempts_workload_id"),
        "workload_observation_attempts",
        ["workload_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workload_observation_attempts_attempted_at"),
        "workload_observation_attempts",
        ["attempted_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_workload_observation_attempts_attempted_at"),
        table_name="workload_observation_attempts",
    )
    op.drop_index(
        op.f("ix_workload_observation_attempts_workload_id"),
        table_name="workload_observation_attempts",
    )
    op.drop_table("workload_observation_attempts")
    op.drop_index(op.f("ix_workload_observations_observed_at"), table_name="workload_observations")
    op.drop_index(op.f("ix_workload_observations_workload_id"), table_name="workload_observations")
    op.drop_table("workload_observations")
    op.drop_index(
        op.f("ix_workload_observation_states_due_at"),
        table_name="workload_observation_states",
    )
    op.drop_table("workload_observation_states")
