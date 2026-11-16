"""Retain recovery observations independently from original investigation evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_recovery_verification"
down_revision = "0014_recovery_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recovery_verifications",
        sa.Column(
            "execution_id", sa.Uuid(), sa.ForeignKey("recovery_executions.id"), primary_key=True
        ),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("baseline", postgresql.JSONB(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_slot", sa.Integer(), server_default="0", nullable=False),
        sa.Column("samples", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("next_slot >= 0 AND next_slot <= 11", name="verification_slot_bound"),
        sa.CheckConstraint(
            "deadline IS NULL OR deadline > acknowledged_at", name="verification_window"
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM recovery_verifications)")):
        raise RuntimeError(
            "Retained verification evidence and its first finalization time require this schema."
        )
    op.drop_table("recovery_verifications")
