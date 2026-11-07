"""Fenced job checkpoints, permit evidence on attempts and the provider-call boundary."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_job_checkpoints"
down_revision = "0008_evidence_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job_attempts", sa.Column("permit_authority", sa.Uuid(), nullable=True))
    op.add_column("job_attempts", sa.Column("permit_generation", sa.BigInteger(), nullable=True))
    op.create_table(
        "job_checkpoints",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("stage", sa.String(96), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("permit_authority", sa.Uuid(), nullable=False),
        sa.Column("permit_generation", sa.BigInteger(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("carry", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("job_id", "sequence", name="job_checkpoint_sequence"),
        sa.CheckConstraint("stage ~ '^[a-z_]+(:[a-z0-9_.]+)?$'", name="job_checkpoint_stage"),
    )
    op.add_column("investigation_runs", sa.Column("provider_call", sa.String(16), nullable=True))
    op.create_check_constraint(
        "run_provider_call",
        "investigation_runs",
        "provider_call IS NULL OR (provider_call IN ('none', 'in_flight') "
        "AND state IN ('analyzing', 'waiting_for_quota'))",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM job_checkpoints UNION ALL "
                "SELECT 1 FROM investigation_runs WHERE provider_call IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("Collection checkpoints cannot be safely downgraded; restore a backup.")
    op.drop_constraint("run_provider_call", "investigation_runs", type_="check")
    op.drop_column("investigation_runs", "provider_call")
    op.drop_table("job_checkpoints")
    op.drop_column("job_attempts", "permit_generation")
    op.drop_column("job_attempts", "permit_authority")
