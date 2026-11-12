"""Run-scoped methodology references, distinct from telemetry citations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from cloudops.features.knowledge.schemas import (
    KnowledgeApplicability,
    KnowledgeChunkDetail,
    KnowledgeDocumentDetail,
    KnowledgeModel,
    KnowledgeProvenance,
    SymptomFamily,
    RecoveryTemplate,
)


class KnowledgeRunScope(KnowledgeModel):
    incident_id: UUID
    run_id: UUID
    workload_id: UUID
    cluster: str
    namespace: str
    name: str
    workload_uid: str
    kind: Literal["Deployment"] = "Deployment"
    window_start: datetime
    window_end: datetime
    symptom_family: SymptomFamily | None = None
    recovery_template: RecoveryTemplate | None = None


class KnowledgeRef(KnowledgeModel):
    schema_version: Literal["KnowledgeRef/v1"] = "KnowledgeRef/v1"
    kind: Literal["runbook"] = "runbook"
    document_key: str
    document_version_id: UUID
    version: str
    chunk_id: UUID
    chunker_version: str
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GuidanceChunk(KnowledgeModel):
    reference: KnowledgeRef
    title: str
    heading_path: list[str]
    content: str
    source_path: str
    provenance: KnowledgeProvenance
    applicability: KnowledgeApplicability
    score: float


class KnowledgeRetrieval(KnowledgeModel):
    schema_version: Literal["KnowledgeRetrieval/v1"] = "KnowledgeRetrieval/v1"
    mode: Literal["full_text"] = "full_text"
    status: Literal["matched", "partial", "no_matches", "not_indexed", "unavailable"]
    reason: str | None = None
    query: str | None = Field(default=None, max_length=2000)
    query_version: Literal["websearch_english/v1"] = "websearch_english/v1"
    retrieval_version: Literal["full_text/v1"] = "full_text/v1"
    generation_id: UUID | None = None
    source_digest: str | None = None
    chunker_version: str | None = None
    ranking_config_version: str | None = None
    search_config_version: str | None = None
    scope: KnowledgeRunScope | None = None
    chunks: list[GuidanceChunk] = Field(default_factory=list, max_length=10)
    content_bytes: int = Field(default=0, ge=0, le=32768)
    limit: int = Field(default=10, ge=1, le=10)
    truncated: bool = False
    degraded: bool = False
    limitations: list[str] = Field(default_factory=list)


class KnowledgeSelectionView(KnowledgeModel):
    id: UUID
    run_id: UUID
    actor_id: UUID | None
    created_at: datetime
    origin: Literal["manual", "trusted"]
    retrieval: KnowledgeRetrieval


class RunGuidance(KnowledgeModel):
    state: Literal["not_selected", "saved", "unavailable"]
    reason: str | None = None
    selection: KnowledgeSelectionView | None = None


class ManualGuidanceRequest(KnowledgeModel):
    pass


class KnowledgeReferenceTarget(KnowledgeModel):
    reference: KnowledgeRef
    document: KnowledgeDocumentDetail
    chunk: KnowledgeChunkDetail
    selection_id: UUID | None = None
