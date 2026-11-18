"""Bind E02 observations to their reader configuration and index E08 history."""

import sqlalchemy as sa
from alembic import op

revision = "0018_health_evidence"
down_revision = "0017_recommendation_reports"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("attempt_binding", "success_binding", "failure_binding"):
        op.add_column("workload_observation_state", sa.Column(column, sa.String(64), nullable=True))
    op.create_index(
        "ix_workloads_active_health_scope",
        "workloads",
        [sa.text("source DESC"), "cluster", "namespace", "name", "id"],
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "ix_scope_targets_configured_health",
        "workload_scope_targets",
        ["cluster", "namespace", "name"],
        postgresql_where=sa.text("configured"),
    )
    op.create_index(
        "ix_recommendation_reports_qualified_history",
        "recommendation_reports",
        ["finished_at", "id"],
        postgresql_where=sa.text(
            "schema_version = 2 AND state = 'available' AND finished_at IS NOT NULL"
        ),
    )


def downgrade():
    op.drop_index(
        "ix_recommendation_reports_qualified_history", table_name="recommendation_reports"
    )
    op.drop_index("ix_scope_targets_configured_health", table_name="workload_scope_targets")
    op.drop_index("ix_workloads_active_health_scope", table_name="workloads")
    for column in ("failure_binding", "success_binding", "attempt_binding"):
        op.drop_column("workload_observation_state", column)
