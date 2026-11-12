"""pin immutable run guidance selections"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_knowledge_selections"
down_revision = "0010_knowledge_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_selections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("consumer", sa.String(length=32), nullable=False),
        sa.Column("request_identity", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["knowledge_generations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "generation_id", name="knowledge_selection_generation"),
        sa.UniqueConstraint(
            "run_id", "consumer", "request_identity", name="knowledge_selection_request"
        ),
    )
    op.create_index("ix_knowledge_selections_run", "knowledge_selections", ["run_id"], unique=False)
    op.create_table(
        "knowledge_selection_chunks",
        sa.Column("selection_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND ordinal < 10", name="knowledge_selection_chunk_ordinal"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "chunk_id"],
            ["knowledge_generation_chunks.generation_id", "knowledge_generation_chunks.chunk_id"],
            name="knowledge_selection_chunk_member",
        ),
        sa.ForeignKeyConstraint(
            ["selection_id", "generation_id"],
            ["knowledge_selections.id", "knowledge_selections.generation_id"],
            name="knowledge_selection_chunk_owner",
        ),
        sa.PrimaryKeyConstraint("selection_id", "ordinal"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM knowledge_selections LIMIT 1")).first():
        raise RuntimeError("Saved Run guidance cannot be safely downgraded; restore a backup.")
    op.drop_table("knowledge_selection_chunks")
    op.drop_index("ix_knowledge_selections_run", table_name="knowledge_selections")
    op.drop_table("knowledge_selections")
