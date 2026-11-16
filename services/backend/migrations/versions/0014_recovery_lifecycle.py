"""Retain immutable call admission and reconciliation budgets without rewriting history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_recovery_lifecycle"
down_revision = "0013_recovery_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "recovery_executions", sa.Column("dispatch_request", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "recovery_executions",
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recovery_executions",
        sa.Column("reconcile_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recovery_executions",
        sa.Column("reconcile_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "reconcile_attempts_nonnegative", "recovery_executions", "reconcile_attempts >= 0"
    )
    op.execute("""
        UPDATE recovery_executions AS execution
        SET dispatch_started_at = CURRENT_TIMESTAMP,
            reconcile_deadline = CURRENT_TIMESTAMP + INTERVAL '120 seconds'
        WHERE execution.state IN ('executing', 'verifying', 'reconciling', 'rolling_back', 'outcome_unknown')
           OR EXISTS (
                SELECT 1 FROM jobs
                WHERE kind = 'recovery_execution_v1' AND subject_id = execution.id
                  AND (state = 'running' OR attempts > 0)
           )
    """)


def downgrade():
    if op.get_bind().scalar(
        sa.text("""
            SELECT EXISTS (SELECT 1 FROM recovery_executions
            WHERE dispatch_request IS NOT NULL OR dispatch_started_at IS NOT NULL
               OR reconcile_attempts > 0)
        """)
    ):
        raise RuntimeError(
            "Retained call admission or reconciliation evidence requires this schema; keep the original authority and use a compatible reader."
        )
    op.drop_constraint("reconcile_attempts_nonnegative", "recovery_executions", type_="check")
    for name in (
        "reconcile_attempts",
        "reconcile_deadline",
        "dispatch_started_at",
        "dispatch_request",
    ):
        op.drop_column("recovery_executions", name)
