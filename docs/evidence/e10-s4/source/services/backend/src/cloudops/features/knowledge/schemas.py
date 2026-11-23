"""Public Knowledge read DTOs and index status projection."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


SymptomFamily = Literal[
    "crashloop_startup",
    "oom_restarts",
    "traffic_latency_errors",
    "pending_unschedulable",
    "image_pull",
]
RecoveryTemplate = Literal["restore_deployment_template", "set_memory_profile", "scale_deployment"]


class KnowledgeApplicability(KnowledgeModel):
    kind: Literal["Deployment"]
    symptom_families: list[SymptomFamily]
    recovery_templates: list[RecoveryTemplate]


class KnowledgeProvenance(KnowledgeModel):
    source_revision: str
    reviewed_at: date


class KnowledgeDocumentSummary(KnowledgeModel):
    id: UUID
    document_key: str
    version: str
    title: str
    guide_kind: Literal["diagnosis", "recovery_template"]
    tags: list[str]
    applicability: KnowledgeApplicability
    source_path: str
    provenance: KnowledgeProvenance
    content_sha256: str
    metadata_sha256: str
    bytes: int


class KnowledgeDocumentPage(KnowledgeModel):
    items: list[KnowledgeDocumentSummary]
    next_cursor: str | None
    generation_id: UUID | None
    source_digest: str | None
    has_active_generation: bool
    catalog_empty_reason: str | None = None


class KnowledgeDocumentDetail(KnowledgeDocumentSummary):
    content: str
    canonicalization_version: str


class KnowledgeChunkDetail(KnowledgeModel):
    id: UUID
    document_version_id: UUID
    ordinal: int
    chunker_version: str
    heading_path: list[str]
    heading_label: str | None
    heading_locator: str
    start_byte: int
    end_byte: int
    content: str
    content_sha256: str
    bytes: int


class KnowledgeAttemptView(KnowledgeModel):
    id: UUID
    phase: str
    outcome: str | None
    started_at: datetime
    finished_at: datetime | None
    source_digest: str | None
    generation_fingerprint: str | None
    document_count: int | None
    chunk_count: int | None
    reason: str | None


class KnowledgeGenerationView(KnowledgeModel):
    id: UUID
    fingerprint: str
    source_digest: str
    chunker_version: str
    index_format_version: str
    search_config_version: str
    ranking_config_version: str
    config_digest: str
    document_count: int
    chunk_count: int
    captured_at: datetime
    indexed_at: datetime


class KnowledgeRetainedCounts(KnowledgeModel):
    document_versions: int
    chunks: int
    generations: int


class KnowledgeIndexStatus(KnowledgeModel):
    state: Literal["not_indexed", "updating", "degraded", "ready"]
    observed_at: datetime
    active_generation: KnowledgeGenerationView | None
    last_attempt: KnowledgeAttemptView | None
    last_success: KnowledgeAttemptView | None
    last_checked_source_digest: str | None
    last_checked_at: datetime | None
    last_success_at: datetime | None
    has_active_generation: bool
    read_available: bool
    catalog_search_available: bool
    retrieval_available: bool
    retrieval_mode: Literal["catalog_full_text"]
    semantic_mode: Literal["not_configured"]
    retained: KnowledgeRetainedCounts


class KnowledgeIndexRequestResult(KnowledgeModel):
    outcome: Literal["indexed", "unchanged", "reactivated", "index_busy", "failed"]
    generation_id: UUID | None = None
    attempt_id: UUID | None = None
    source_digest: str | None = None
    document_count: int | None = None
    chunk_count: int | None = None
    reason: str | None = Field(default=None, max_length=512)
