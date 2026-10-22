"""Persist the local worker heartbeat."""

from alembic import op
import sqlalchemy as sa

revision = "0001_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
