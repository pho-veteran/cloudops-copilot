"""Retain first-finalization inverse windows without inventing legacy authority."""

from alembic import op
import sqlalchemy as sa

revision = "0016_recovery_inverse"
down_revision = "0015_recovery_verification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("action_plans", sa.Column("forward_execution_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "action_plans_forward_execution_id_fkey",
        "action_plans",
        "recovery_executions",
        ["forward_execution_id"],
        ["id"],
    )
    op.create_index(
        "ix_action_plans_forward_execution_id", "action_plans", ["forward_execution_id"]
    )
    op.create_table(
        "recovery_rollback_windows",
        sa.Column(
            "forward_execution_id",
            sa.Uuid(),
            sa.ForeignKey("recovery_executions.id"),
            primary_key=True,
        ),
        sa.Column("eligible_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column(
            "inverse_execution_id", sa.Uuid(), sa.ForeignKey("recovery_executions.id"), unique=True
        ),
        sa.CheckConstraint("deadline > eligible_from", name="rollback_window_positive"),
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM recovery_rollback_windows)")
    ) or connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM action_plans WHERE forward_execution_id IS NOT NULL)")
    ):
        raise RuntimeError("Retained inverse authority and linkage require this schema.")
    op.drop_table("recovery_rollback_windows")
    op.drop_index("ix_action_plans_forward_execution_id", table_name="action_plans")
    op.drop_constraint("action_plans_forward_execution_id_fkey", "action_plans", type_="foreignkey")
    op.drop_column("action_plans", "forward_execution_id")
