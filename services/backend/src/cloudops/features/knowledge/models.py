"""Immutable runbook versions, chunks, generations and the mutable index control row."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint("document_key", "version", name="knowledge_document_version_identity"),
        Index("ix_knowledge_document_versions_key", "document_key"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    guide_kind: Mapped[str] = mapped_column(String(32))
    tags: Mapped[list] = mapped_column(JSONB)
    applicability: Mapped[dict] = mapped_column(JSONB)
    source_path: Mapped[str] = mapped_column(String(256))
    provenance: Mapped[dict] = mapped_column(JSONB)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    content_sha256: Mapped[str] = mapped_column(String(64))
    metadata_sha256: Mapped[str] = mapped_column(String(64))
    canonicalization_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "chunker_version",
            "ordinal",
            "heading_locator",
            "content_sha256",
            name="knowledge_chunk_identity",
        ),
        UniqueConstraint("id", "document_version_id", name="knowledge_chunk_document_identity"),
        CheckConstraint("ordinal >= 0", name="knowledge_chunk_ordinal"),
        CheckConstraint(
            "start_byte >= 0 AND end_byte > start_byte", name="knowledge_chunk_offsets"
        ),
        Index("ix_knowledge_chunks_document", "document_version_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_document_versions.id"))
    chunker_version: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list] = mapped_column(JSONB)
    heading_label: Mapped[str | None] = mapped_column(String(200))
    heading_locator: Mapped[str] = mapped_column(String(512))
    start_byte: Mapped[int] = mapped_column(BigInteger)
    end_byte: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeGeneration(Base):
    __tablename__ = "knowledge_generations"
    __table_args__ = (UniqueConstraint("fingerprint", name="knowledge_generation_fingerprint"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    chunker_version: Mapped[str] = mapped_column(String(32))
    index_format_version: Mapped[str] = mapped_column(String(32))
    search_config_version: Mapped[str] = mapped_column(String(32))
    ranking_config_version: Mapped[str] = mapped_column(String(32))
    config_digest: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSONB)
    document_count: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeGenerationDocument(Base):
    __tablename__ = "knowledge_generation_documents"
    __table_args__ = (
        Index(
            "ix_knowledge_generation_documents_search",
            "search_vector",
            postgresql_using="gin",
        ),
    )
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_generations.id"), primary_key=True
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_document_versions.id"), primary_key=True
    )
    search_vector: Mapped[object] = mapped_column(TSVECTOR)


class KnowledgeGenerationChunk(Base):
    __tablename__ = "knowledge_generation_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "document_version_id"],
            [
                "knowledge_generation_documents.generation_id",
                "knowledge_generation_documents.document_version_id",
            ],
            name="knowledge_generation_chunk_document",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "document_version_id"],
            ["knowledge_chunks.id", "knowledge_chunks.document_version_id"],
            name="knowledge_generation_chunk_identity",
        ),
        Index(
            "ix_knowledge_generation_chunks_search",
            "search_vector",
            postgresql_using="gin",
        ),
    )
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_generations.id"), primary_key=True
    )
    chunk_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_chunks.id"), primary_key=True)
    document_version_id: Mapped[UUID] = mapped_column()
    search_vector: Mapped[object] = mapped_column(TSVECTOR)


class KnowledgeIndexAttempt(Base):
    __tablename__ = "knowledge_index_attempts"
    __table_args__ = (Index("ix_knowledge_index_attempts_started", "started_at"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    fence: Mapped[int] = mapped_column(BigInteger)
    phase: Mapped[str] = mapped_column(String(32), default="starting")
    outcome: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_digest: Mapped[str | None] = mapped_column(String(64))
    generation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    document_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(512))


class KnowledgeIndexState(Base):
    __tablename__ = "knowledge_index_state"
    __table_args__ = (CheckConstraint("id = 1", name="knowledge_index_state_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    active_generation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_generations.id")
    )
    current_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_index_attempts.id")
    )
    last_success_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_index_attempts.id")
    )
    fence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_checked_source_digest: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(32))
    last_reason: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeSelection(Base):
    __tablename__ = "knowledge_selections"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "consumer", "request_identity", name="knowledge_selection_request"
        ),
        UniqueConstraint("id", "generation_id", name="knowledge_selection_generation"),
        Index("ix_knowledge_selections_run", "run_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("investigation_runs.id"))
    generation_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_generations.id"))
    consumer: Mapped[str] = mapped_column(String(32))
    request_identity: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeSelectionChunk(Base):
    __tablename__ = "knowledge_selection_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["selection_id", "generation_id"],
            ["knowledge_selections.id", "knowledge_selections.generation_id"],
            name="knowledge_selection_chunk_owner",
        ),
        ForeignKeyConstraint(
            ["generation_id", "chunk_id"],
            ["knowledge_generation_chunks.generation_id", "knowledge_generation_chunks.chunk_id"],
            name="knowledge_selection_chunk_member",
        ),
        CheckConstraint("ordinal >= 0 AND ordinal < 10", name="knowledge_selection_chunk_ordinal"),
    )
    selection_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[UUID] = mapped_column()
    chunk_id: Mapped[UUID] = mapped_column()
