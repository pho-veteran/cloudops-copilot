"""Knowledge request fixtures for the permission matrix."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func

from cloudops.features.knowledge.chunking import chunk_document
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeGeneration,
    KnowledgeGenerationChunk,
    KnowledgeGenerationDocument,
    KnowledgeIndexAttempt,
    KnowledgeIndexState,
)
from cloudops.features.knowledge.schemas import (
    KnowledgeChunkDetail,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeIndexStatus,
)
from cloudops.shared.database import sessions
from permission_cases import RequestCase, validated

CONTENT = b"# Fixture guide\n\nFixture body.\n"
SOURCE_DIGEST = "a" * 64
FINGERPRINT = "b" * 64


def seed(context, _tmp_path):
    """Insert a complete immutable generation without the expensive writer path."""
    chunk = chunk_document(CONTENT)[0]
    now = datetime.now(timezone.utc)
    document_id = uuid4()
    generation_id = uuid4()
    chunk_id = uuid4()
    attempt_id = uuid4()
    with sessions()() as db, db.begin():
        document = KnowledgeDocumentVersion(
            id=document_id,
            document_key="fixture-guide",
            version="1",
            title="Fixture guide",
            guide_kind="diagnosis",
            tags=["fixture", "test"],
            applicability={
                "kind": "Deployment",
                "symptom_families": [],
                "recovery_templates": [],
            },
            source_path="runbooks/fixture-guide.md",
            provenance={
                "source_revision": "6c8eea37e982f3dc6de2755e7a482eb16aaf1f16",
                "reviewed_at": "2026-11-11",
            },
            content=CONTENT,
            content_sha256="c" * 64,
            metadata_sha256="d" * 64,
            canonicalization_version="1",
        )
        generation = KnowledgeGeneration(
            id=generation_id,
            fingerprint=FINGERPRINT,
            source_digest=SOURCE_DIGEST,
            chunker_version="1",
            index_format_version="1",
            search_config_version="1",
            ranking_config_version="1",
            config_digest="e" * 64,
            config={"fixture": True},
            document_count=1,
            chunk_count=1,
            captured_at=now,
            indexed_at=now,
        )
        chunk_row = KnowledgeChunk(
            id=chunk_id,
            document_version_id=document_id,
            chunker_version="1",
            ordinal=0,
            heading_path=list(chunk.heading_path),
            heading_label=chunk.heading_label,
            heading_locator=chunk.heading_locator,
            start_byte=chunk.start_byte,
            end_byte=chunk.end_byte,
            content=chunk.content,
            content_sha256=chunk.content_sha256,
        )
        attempt = KnowledgeIndexAttempt(
            id=attempt_id,
            fence=1,
            phase="activated",
            outcome="indexed",
            started_at=now,
            finished_at=now,
            source_digest=SOURCE_DIGEST,
            generation_fingerprint=FINGERPRINT,
            document_count=1,
            chunk_count=1,
        )
        state = KnowledgeIndexState(
            id=1,
            active_generation_id=generation_id,
            last_success_attempt_id=attempt_id,
            fence=1,
            last_checked_source_digest=SOURCE_DIGEST,
            last_checked_at=now,
            last_success_at=now,
            last_outcome="indexed",
        )
        db.add_all([document, generation, attempt])
        db.flush()
        db.add(chunk_row)
        db.flush()
        db.add(state)
        db.flush()
        db.add(
            KnowledgeGenerationDocument(
                generation_id=generation_id,
                document_version_id=document_id,
                search_vector=func.to_tsvector("english", "Fixture guide Fixture body."),
            )
        )
        db.flush()
        db.add(
            KnowledgeGenerationChunk(
                generation_id=generation_id,
                chunk_id=chunk_id,
                document_version_id=document_id,
                search_vector=func.to_tsvector("english", "Fixture guide Fixture body."),
            )
        )
    context.knowledge_document_id = str(document_id)
    context.knowledge_chunk_id = str(chunk_id)
    context.knowledge_generation_id = str(generation_id)


def list_request(context):
    return {}


def list_check(response, context, actor):
    page = validated(KnowledgeDocumentPage, response)
    assert page.has_active_generation
    assert context.knowledge_document_id in {str(item.id) for item in page.items}


def document_request(context):
    return {"path_params": {"document_version_id": context.knowledge_document_id}}


def document_check(response, context, actor):
    value = validated(KnowledgeDocumentDetail, response)
    assert str(value.id) == context.knowledge_document_id
    assert value.content


def chunk_request(context):
    return {
        "path_params": {"chunk_id": context.knowledge_chunk_id},
        "params": {"document_version_id": context.knowledge_document_id},
    }


def chunk_check(response, context, actor):
    value = validated(KnowledgeChunkDetail, response)
    assert str(value.id) == context.knowledge_chunk_id
    assert str(value.document_version_id) == context.knowledge_document_id
    assert value.content


def status_check(response, context, actor):
    value = validated(KnowledgeIndexStatus, response)
    assert value.has_active_generation
    assert value.read_available
    assert value.catalog_search_available


CASES = {
    "list_knowledge_documents": RequestCase(list_request, list_check),
    "read_knowledge_document": RequestCase(document_request, document_check),
    "read_knowledge_chunk": RequestCase(chunk_request, chunk_check),
    "get_knowledge_index_status": RequestCase(list_request, status_check),
}
