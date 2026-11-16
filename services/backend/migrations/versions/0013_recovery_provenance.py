"""Preserve legacy recovery bytes while adding explicit proposal origin."""

from alembic import op
import sqlalchemy as sa

revision = "0013_recovery_provenance"
down_revision = "0012_diagnosis_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("action_plans", sa.Column("origin", sa.String(32), nullable=True))
    op.add_column("action_plans", sa.Column("source_analysis_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "action_plan_analysis", "action_plans", "diagnosis_analyses", ["source_analysis_id"], ["id"]
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM action_plans WHERE content->>'schema_version' = 'RecoveryPlan/v2')"
        )
    ):
        raise RuntimeError(
            "Retained v2 plans require this schema; downgrade would remove their provenance and reader compatibility."
        )
    op.drop_constraint("action_plan_analysis", "action_plans", type_="foreignkey")
    op.drop_column("action_plans", "source_analysis_id")
    op.drop_column("action_plans", "origin")
