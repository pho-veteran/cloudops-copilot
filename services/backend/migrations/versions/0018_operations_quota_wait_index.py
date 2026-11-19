"""Index the bounded operations sample of quota-waiting runs."""

import sqlalchemy as sa
from alembic import op

revision = "0018_operations_quota_wait_index"
down_revision = "0017_recommendation_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_runs_quota_wait",
        "investigation_runs",
        ["created_at", "id"],
        postgresql_where=sa.text("state = 'waiting_for_quota'"),
    )


def downgrade():
    op.drop_index("ix_runs_quota_wait", table_name="investigation_runs")
