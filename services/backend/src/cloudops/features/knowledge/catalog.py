"""Read-only catalog, exact historical reads and index status projection."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cloudops.features.knowledge.index_guard import lock_present
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeGeneration,
    KnowledgeGenerationDocument,
    KnowledgeIndexAttempt,
    KnowledgeIndexState,
)
from cloudops.features.knowledge.schemas import (
    KnowledgeAttemptView,
    KnowledgeChunkDetail,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeDocumentSummary,
    KnowledgeGenerationView,
    KnowledgeIndexStatus,
    KnowledgeRetainedCounts,
)
from cloudops.shared.errors import DomainError


def _summary(row: KnowledgeDocumentVersion) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary(
        id=row.id,
        document_key=row.document_key,
        version=row.version,
        title=row.title,
        guide_kind=row.guide_kind,
        tags=list(row.tags),
        applicability=row.applicability,
        source_path=row.source_path,
        provenance=row.provenance,
        content_sha256=row.content_sha256,
        metadata_sha256=row.metadata_sha256,
        bytes=len(row.content),
    )


def _attempt(
    row: KnowledgeIndexAttempt | None, *, interrupted: bool = False
) -> KnowledgeAttemptView | None:
    if row is None:
        return None
    return KnowledgeAttemptView(
        id=row.id,
        phase=row.phase,
        outcome=row.outcome,
        started_at=row.started_at,
        finished_at=row.finished_at,
        source_digest=row.source_digest,
        generation_fingerprint=row.generation_fingerprint,
        document_count=row.document_count,
        chunk_count=row.chunk_count,
        reason=(
            row.reason
            or ("Index attempt did not finish; retry explicitly." if interrupted else None)
        ),
    )


def _generation(row: KnowledgeGeneration | None) -> KnowledgeGenerationView | None:
    if row is None:
        return None
    return KnowledgeGenerationView(
        id=row.id,
        fingerprint=row.fingerprint,
        source_digest=row.source_digest,
        chunker_version=row.chunker_version,
        index_format_version=row.index_format_version,
        search_config_version=row.search_config_version,
        ranking_config_version=row.ranking_config_version,
        config_digest=row.config_digest,
        document_count=row.document_count,
        chunk_count=row.chunk_count,
        captured_at=row.captured_at,
        indexed_at=row.indexed_at,
    )


def _query_hash(query: str | None) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()


def _tags_hash(tags: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(tags), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _encode_cursor(*, offset: int, generation_id: UUID, query: str | None, tags: list[str]) -> str:
    payload = {
        "v": 1,
        "offset": offset,
        "generation_id": str(generation_id),
        "q": _query_hash(query),
        "tags": _tags_hash(tags),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    if not cursor or len(cursor) > 512:
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.")
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.") from None
    if not isinstance(value, dict) or set(value) != {"v", "offset", "generation_id", "q", "tags"}:
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.")
    if type(value["v"]) is not int or value["v"] != 1:
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.")
    if type(value["offset"]) is not int or not 0 <= value["offset"] <= 100:
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.")
    try:
        UUID(value["generation_id"])
    except (ValueError, TypeError):
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.") from None
    if not isinstance(value["q"], str) or not isinstance(value["tags"], str):
        raise DomainError(422, "invalid_cursor", "The catalog cursor is invalid.")
    return value


def list_documents(
    db: Session,
    *,
    query: str | None,
    tags: list[str],
    limit: int,
    cursor: str | None,
) -> KnowledgeDocumentPage:
    if query is not None and len(query) > 2000:
        raise DomainError(422, "invalid_query", "Search text is too long.")
    if len(tags) > 16 or any(not tag or len(tag) > 64 for tag in tags):
        raise DomainError(422, "invalid_tags", "Tags are invalid or exceed the bound.")
    state = db.get(KnowledgeIndexState, 1)
    generation_id = state.active_generation_id if state else None
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if generation_id is None or decoded["generation_id"] != str(generation_id):
            raise DomainError(
                409,
                "catalog_changed",
                "The active catalog changed. Start from the first page.",
            )
        if decoded["q"] != _query_hash(query) or decoded["tags"] != _tags_hash(tags):
            raise DomainError(
                422, "invalid_cursor", "The catalog cursor does not match this query."
            )
        offset = decoded["offset"]
    if generation_id is None:
        return KnowledgeDocumentPage(
            items=[],
            next_cursor=None,
            generation_id=None,
            source_digest=None,
            has_active_generation=False,
            catalog_empty_reason="not_indexed",
        )
    generation = db.get(KnowledgeGeneration, generation_id)
    if generation is None:
        return KnowledgeDocumentPage(
            items=[],
            next_cursor=None,
            generation_id=None,
            source_digest=None,
            has_active_generation=False,
            catalog_empty_reason="not_indexed",
        )
    statement = (
        select(KnowledgeDocumentVersion)
        .join(
            KnowledgeGenerationDocument,
            KnowledgeGenerationDocument.document_version_id == KnowledgeDocumentVersion.id,
        )
        .where(KnowledgeGenerationDocument.generation_id == generation_id)
    )
    for tag in tags:
        statement = statement.where(KnowledgeDocumentVersion.tags.contains([tag]))
    if query:
        tsquery = func.websearch_to_tsquery("english", query)
        statement = statement.where(KnowledgeGenerationDocument.search_vector.op("@@")(tsquery))
    statement = (
        statement.order_by(
            KnowledgeDocumentVersion.document_key,
            KnowledgeDocumentVersion.version,
            KnowledgeDocumentVersion.id,
        )
        .limit(limit + 1)
        .offset(offset)
    )
    rows = list(db.scalars(statement))
    page_rows = rows[:limit]
    next_offset = offset + limit if len(rows) > limit else None
    next_cursor = (
        _encode_cursor(
            offset=next_offset,
            generation_id=generation_id,
            query=query,
            tags=tags,
        )
        if next_offset is not None
        else None
    )
    reason = None
    if not page_rows and offset == 0:
        reason = "no_matches" if query or tags else "empty_catalog"
    return KnowledgeDocumentPage(
        items=[_summary(row) for row in page_rows],
        next_cursor=next_cursor,
        generation_id=generation_id,
        source_digest=generation.source_digest,
        has_active_generation=True,
        catalog_empty_reason=reason,
    )


def read_document(db: Session, document_version_id: UUID) -> KnowledgeDocumentDetail:
    row = db.get(KnowledgeDocumentVersion, document_version_id)
    if row is None:
        raise DomainError(404, "document_not_found", "Runbook document not found.")
    try:
        content = row.content.decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError(
            409, "content_unavailable", "Stored document content is invalid."
        ) from None
    return KnowledgeDocumentDetail(
        **_summary(row).model_dump(),
        content=content,
        canonicalization_version=row.canonicalization_version,
    )


def read_chunk(db: Session, chunk_id: UUID, document_version_id: UUID) -> KnowledgeChunkDetail:
    row = db.get(KnowledgeChunk, chunk_id)
    if row is None or row.document_version_id != document_version_id:
        raise DomainError(404, "chunk_not_found", "Runbook chunk not found for this document.")
    try:
        content = row.content.decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError(409, "content_unavailable", "Stored chunk content is invalid.") from None
    return KnowledgeChunkDetail(
        id=row.id,
        document_version_id=row.document_version_id,
        ordinal=row.ordinal,
        chunker_version=row.chunker_version,
        heading_path=list(row.heading_path),
        heading_label=row.heading_label,
        heading_locator=row.heading_locator,
        start_byte=row.start_byte,
        end_byte=row.end_byte,
        content=content,
        content_sha256=row.content_sha256,
        bytes=len(row.content),
    )


def index_status(db: Session) -> KnowledgeIndexStatus:
    # Probe the lock on a separate connection before loading projection rows;
    # this keeps the read session untouched and one coherent snapshot.
    updating = lock_present(db)
    state = db.get(KnowledgeIndexState, 1)
    active = (
        db.get(KnowledgeGeneration, state.active_generation_id)
        if state and state.active_generation_id
        else None
    )
    latest = db.scalar(
        select(KnowledgeIndexAttempt)
        .order_by(KnowledgeIndexAttempt.started_at.desc(), KnowledgeIndexAttempt.id.desc())
        .limit(1)
    )
    last_success = (
        db.get(KnowledgeIndexAttempt, state.last_success_attempt_id)
        if state and state.last_success_attempt_id
        else None
    )
    retained = KnowledgeRetainedCounts(
        document_versions=db.scalar(select(func.count()).select_from(KnowledgeDocumentVersion))
        or 0,
        chunks=db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0,
        generations=db.scalar(select(func.count()).select_from(KnowledgeGeneration)) or 0,
    )
    unfinished = latest is not None and latest.outcome is None
    if updating:
        status = "updating"
    elif active is None:
        status = "not_indexed"
    elif unfinished or (state and state.last_outcome in {"failed", "interrupted", "guard_lost"}):
        status = "degraded"
    else:
        status = "ready"
    active_count = active is not None
    return KnowledgeIndexStatus(
        state=status,
        observed_at=datetime.now(timezone.utc),
        active_generation=_generation(active),
        last_attempt=_attempt(latest, interrupted=unfinished),
        last_success=_attempt(last_success),
        last_checked_source_digest=state.last_checked_source_digest if state else None,
        last_checked_at=state.last_checked_at if state else None,
        last_success_at=state.last_success_at if state else None,
        has_active_generation=active_count,
        read_available=active_count,
        catalog_search_available=active_count,
        retrieval_available=active_count,
        retrieval_mode="catalog_full_text",
        semantic_mode="not_configured",
        retained=retained,
    )
