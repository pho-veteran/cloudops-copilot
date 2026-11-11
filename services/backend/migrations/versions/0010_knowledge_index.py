"""Immutable knowledge corpus versions, chunks, generations and index control."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_knowledge_index"
down_revision = "0009_job_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_key", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("guide_kind", sa.String(32), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.Column("applicability", postgresql.JSONB(), nullable=False),
        sa.Column("source_path", sa.String(256), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("metadata_sha256", sa.String(64), nullable=False),
        sa.Column("canonicalization_version", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("document_key", "version", name="knowledge_document_version_identity"),
    )
    op.create_index(
        "ix_knowledge_document_versions_key",
        "knowledge_document_versions",
        ["document_key"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_document_versions.id"),
            nullable=False,
        ),
        sa.Column("chunker_version", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(), nullable=False),
        sa.Column("heading_label", sa.String(200), nullable=True),
        sa.Column("heading_locator", sa.String(512), nullable=False),
        sa.Column("start_byte", sa.BigInteger(), nullable=False),
        sa.Column("end_byte", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="knowledge_chunk_ordinal"),
        sa.CheckConstraint(
            "start_byte >= 0 AND end_byte > start_byte", name="knowledge_chunk_offsets"
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "chunker_version",
            "ordinal",
            "heading_locator",
            "content_sha256",
            name="knowledge_chunk_identity",
        ),
        sa.UniqueConstraint("id", "document_version_id", name="knowledge_chunk_document_identity"),
    )
    op.create_index("ix_knowledge_chunks_document", "knowledge_chunks", ["document_version_id"])

    op.create_table(
        "knowledge_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("chunker_version", sa.String(32), nullable=False),
        sa.Column("index_format_version", sa.String(32), nullable=False),
        sa.Column("search_config_version", sa.String(32), nullable=False),
        sa.Column("ranking_config_version", sa.String(32), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fingerprint", name="knowledge_generation_fingerprint"),
    )

    op.create_table(
        "knowledge_generation_documents",
        sa.Column(
            "generation_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_generations.id"),
            primary_key=True,
        ),
        sa.Column(
            "document_version_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_document_versions.id"),
            primary_key=True,
        ),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False),
    )
    op.create_index(
        "ix_knowledge_generation_documents_search",
        "knowledge_generation_documents",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "knowledge_generation_chunks",
        sa.Column(
            "generation_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_generations.id"),
            primary_key=True,
        ),
        sa.Column("chunk_id", sa.Uuid(), sa.ForeignKey("knowledge_chunks.id"), primary_key=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_id", "document_version_id"],
            [
                "knowledge_generation_documents.generation_id",
                "knowledge_generation_documents.document_version_id",
            ],
            name="knowledge_generation_chunk_document",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "document_version_id"],
            ["knowledge_chunks.id", "knowledge_chunks.document_version_id"],
            name="knowledge_generation_chunk_identity",
        ),
    )
    op.create_index(
        "ix_knowledge_generation_chunks_search",
        "knowledge_generation_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "knowledge_index_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_digest", sa.String(64), nullable=True),
        sa.Column("generation_fingerprint", sa.String(64), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(512), nullable=True),
    )
    op.create_index(
        "ix_knowledge_index_attempts_started", "knowledge_index_attempts", ["started_at"]
    )

    op.create_table(
        "knowledge_index_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "active_generation_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_generations.id"),
            nullable=True,
        ),
        sa.Column(
            "current_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_index_attempts.id"),
            nullable=True,
        ),
        sa.Column(
            "last_success_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_index_attempts.id"),
            nullable=True,
        ),
        sa.Column("fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_checked_source_digest", sa.String(64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(32), nullable=True),
        sa.Column("last_reason", sa.String(512), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="knowledge_index_state_singleton"),
    )


def downgrade():
    bind = op.get_bind()
    for table in (
        "knowledge_document_versions",
        "knowledge_chunks",
        "knowledge_generations",
        "knowledge_generation_documents",
        "knowledge_generation_chunks",
        "knowledge_index_attempts",
        "knowledge_index_state",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Knowledge history cannot be safely downgraded; restore a backup.")
    op.drop_table("knowledge_index_state")
    op.drop_index("ix_knowledge_index_attempts_started", table_name="knowledge_index_attempts")
    op.drop_table("knowledge_index_attempts")
    op.drop_index("ix_knowledge_generation_chunks_search", table_name="knowledge_generation_chunks")
    op.drop_table("knowledge_generation_chunks")
    op.drop_index(
        "ix_knowledge_generation_documents_search",
        table_name="knowledge_generation_documents",
    )
    op.drop_table("knowledge_generation_documents")
    op.drop_table("knowledge_generations")
    op.drop_index("ix_knowledge_chunks_document", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_document_versions_key", table_name="knowledge_document_versions")
    op.drop_table("knowledge_document_versions")
