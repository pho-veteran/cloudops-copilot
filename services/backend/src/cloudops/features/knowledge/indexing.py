"""Validated capture, deterministic chunking and atomic immutable activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cloudops.features.knowledge.chunking import CHUNKER_VERSION, ChunkingError, chunk_document
from cloudops.features.knowledge.corpus import (
    CANONICALIZATION_VERSION,
    CapturedCorpus,
    CapturedDocument,
    CorpusError,
    capture_corpus,
)
from cloudops.features.knowledge.index_guard import GuardBusy, GuardLost, WriterGuard
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeGeneration,
    KnowledgeGenerationChunk,
    KnowledgeGenerationDocument,
    KnowledgeIndexAttempt,
    KnowledgeIndexState,
)
from cloudops.features.knowledge.schemas import KnowledgeIndexRequestResult

INDEX_FORMAT_VERSION = "1"
SEARCH_CONFIG_VERSION = "1"
RANKING_CONFIG_VERSION = "1"
SEARCH_CONFIG = {
    "chunker": CHUNKER_VERSION,
    "format": INDEX_FORMAT_VERSION,
    "search": SEARCH_CONFIG_VERSION,
    "ranking": RANKING_CONFIG_VERSION,
    "weights": {"title": "A", "heading": "B", "body": "C"},
}
CONFIG_DIGEST = hashlib.sha256(
    json.dumps(SEARCH_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class KnowledgeConflict(ValueError):
    """A same-key/version candidate differs from retained history."""


class KnowledgeFailure(ValueError):
    """A safely reportable indexing failure."""


@dataclass(frozen=True)
class PreparedDocument:
    document: CapturedDocument
    chunks: tuple


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _generation_fingerprint(source_digest: str) -> str:
    return _sha(
        _canonical(
            {
                "source_digest": source_digest,
                "chunker_version": CHUNKER_VERSION,
                "index_format_version": INDEX_FORMAT_VERSION,
                "search_config_version": SEARCH_CONFIG_VERSION,
                "ranking_config_version": RANKING_CONFIG_VERSION,
                "config_digest": CONFIG_DIGEST,
            }
        )
    )


def _vector(title: str, headings: str, body: str):
    return (
        func.setweight(func.to_tsvector("english", title), literal_column("'A'"))
        .op("||")(func.setweight(func.to_tsvector("english", headings), literal_column("'B'")))
        .op("||")(func.setweight(func.to_tsvector("english", body), literal_column("'C'")))
    )


def _document_vector(document: CapturedDocument, chunks: tuple) -> object:
    headings = "\n".join(" / ".join(chunk.heading_path) for chunk in chunks if chunk.heading_path)
    return _vector(document.title, headings, document.content.decode("utf-8"))


def _chunk_vector(chunk) -> object:
    return _vector(
        chunk.heading_label or "",
        " / ".join(chunk.heading_path),
        chunk.content.decode("utf-8"),
    )


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, SQLAlchemyError):
        return "Knowledge database operation failed. Check database availability and retry."
    return (" ".join(str(exc).split()) or "Knowledge index failed.")[:512]


def _prepare(root) -> tuple[CapturedCorpus, tuple[PreparedDocument, ...]]:
    captured = capture_corpus(root)
    prepared = []
    for document in captured.documents:
        try:
            chunks = chunk_document(document.content)
        except ChunkingError as exc:
            raise KnowledgeFailure(str(exc)) from None
        prepared.append(PreparedDocument(document=document, chunks=chunks))
    return captured, tuple(prepared)


def _start_attempt(connection) -> tuple[UUID, int]:
    attempt_id = uuid4()
    with Session(bind=connection) as db, db.begin():
        state = db.scalar(
            select(KnowledgeIndexState).where(KnowledgeIndexState.id == 1).with_for_update()
        )
        if state is None:
            state = KnowledgeIndexState(id=1, fence=0)
            db.add(state)
            db.flush()
        db.execute(
            KnowledgeIndexAttempt.__table__.update()
            .where(KnowledgeIndexAttempt.outcome.is_(None))
            .values(
                outcome="interrupted",
                phase="interrupted",
                finished_at=datetime.now(timezone.utc),
                reason="Previous index invocation did not finish.",
            )
        )
        now = datetime.now(timezone.utc)
        if state.last_outcome is None or state.last_outcome == "interrupted":
            pass
        elif state.current_attempt_id is not None:
            state.last_outcome = "interrupted"
            state.last_reason = "Previous index invocation did not finish."
            state.last_checked_at = now
        state.fence = (state.fence or 0) + 1
        attempt = KnowledgeIndexAttempt(
            id=attempt_id,
            fence=state.fence,
            phase="capturing",
            outcome=None,
        )
        db.add(attempt)
        db.flush()
        state.current_attempt_id = attempt.id
        state.updated_at = now
        return attempt_id, state.fence


def _finish_attempt(
    connection,
    *,
    attempt_id: UUID,
    fence: int,
    outcome: str,
    phase: str,
    source_digest: str | None = None,
    fingerprint: str | None = None,
    document_count: int | None = None,
    chunk_count: int | None = None,
    reason: str | None = None,
) -> None:
    with Session(bind=connection) as db, db.begin():
        state = db.scalar(
            select(KnowledgeIndexState).where(KnowledgeIndexState.id == 1).with_for_update()
        )
        attempt = db.get(KnowledgeIndexAttempt, attempt_id)
        if state is None or attempt is None:
            return
        if state.current_attempt_id != attempt_id or attempt.fence != fence:
            return
        now = datetime.now(timezone.utc)
        attempt.phase = phase
        attempt.outcome = outcome
        attempt.finished_at = now
        attempt.source_digest = source_digest
        attempt.generation_fingerprint = fingerprint
        attempt.document_count = document_count
        attempt.chunk_count = chunk_count
        attempt.reason = reason
        state.current_attempt_id = None
        if source_digest is not None:
            state.last_checked_source_digest = source_digest
        state.last_checked_at = now
        state.last_outcome = outcome
        state.last_reason = reason
        state.last_success_at = (
            now if outcome in {"indexed", "unchanged", "reactivated"} else state.last_success_at
        )
        state.updated_at = now


def _fail(
    connection, attempt_id: UUID, fence: int, reason: str, *, source_digest: str | None = None
) -> None:
    try:
        _finish_attempt(
            connection,
            attempt_id=attempt_id,
            fence=fence,
            outcome="failed",
            phase="failed",
            source_digest=source_digest,
            reason=reason[:512],
        )
    except Exception:
        pass


def _conflict(db: Session, prepared: PreparedDocument) -> None:
    document = prepared.document
    current = db.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_key == document.document_key,
            KnowledgeDocumentVersion.version == document.version,
        )
    )
    if current is None:
        return
    if (
        current.content_sha256 != document.content_sha256
        or current.metadata_sha256 != document.metadata_sha256
        or current.canonicalization_version != CANONICALIZATION_VERSION
    ):
        raise KnowledgeConflict(
            f"Same key/version has different content or metadata: {document.document_key} v{document.version}"
        )


def _document_row(db: Session, document: CapturedDocument) -> KnowledgeDocumentVersion:
    row = db.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_key == document.document_key,
            KnowledgeDocumentVersion.version == document.version,
        )
    )
    if row is not None:
        return row
    row = KnowledgeDocumentVersion(
        document_key=document.document_key,
        version=document.version,
        title=document.title,
        guide_kind=document.guide_kind,
        tags=list(document.tags),
        applicability=document.applicability,
        source_path=document.source_path,
        provenance=document.provenance,
        content=document.content,
        content_sha256=document.content_sha256,
        metadata_sha256=document.metadata_sha256,
        canonicalization_version=CANONICALIZATION_VERSION,
    )
    db.add(row)
    db.flush()
    return row


def _chunk_rows(db: Session, document_row: KnowledgeDocumentVersion, prepared: PreparedDocument):
    rows = []
    for chunk in prepared.chunks:
        row = db.scalar(
            select(KnowledgeChunk).where(
                KnowledgeChunk.document_version_id == document_row.id,
                KnowledgeChunk.chunker_version == CHUNKER_VERSION,
                KnowledgeChunk.ordinal == chunk.ordinal,
                KnowledgeChunk.heading_locator == chunk.heading_locator,
                KnowledgeChunk.content_sha256 == chunk.content_sha256,
            )
        )
        if row is None:
            row = KnowledgeChunk(
                document_version_id=document_row.id,
                chunker_version=CHUNKER_VERSION,
                ordinal=chunk.ordinal,
                heading_path=list(chunk.heading_path),
                heading_label=chunk.heading_label,
                heading_locator=chunk.heading_locator,
                start_byte=chunk.start_byte,
                end_byte=chunk.end_byte,
                content=chunk.content,
                content_sha256=chunk.content_sha256,
            )
            db.add(row)
            db.flush()
        rows.append((row, chunk))
    return tuple(rows)


def _activate(
    connection,
    *,
    guard: WriterGuard,
    captured: CapturedCorpus,
    prepared: tuple[PreparedDocument, ...],
    attempt_id: UUID,
    fence: int,
) -> KnowledgeIndexRequestResult:
    fingerprint = _generation_fingerprint(captured.source_digest)
    with Session(bind=connection) as db, db.begin():
        state = db.scalar(
            select(KnowledgeIndexState).where(KnowledgeIndexState.id == 1).with_for_update()
        )
        if state is None or state.current_attempt_id != attempt_id or state.fence != fence:
            raise GuardLost("Knowledge writer attempt is no longer current.")
        guard.verify(commit=False)
        for item in prepared:
            _conflict(db, item)
        generation = db.scalar(
            select(KnowledgeGeneration).where(KnowledgeGeneration.fingerprint == fingerprint)
        )
        created_generation = generation is None
        changed = False
        if created_generation:
            now = datetime.now(timezone.utc)
            generation = KnowledgeGeneration(
                fingerprint=fingerprint,
                source_digest=captured.source_digest,
                chunker_version=CHUNKER_VERSION,
                index_format_version=INDEX_FORMAT_VERSION,
                search_config_version=SEARCH_CONFIG_VERSION,
                ranking_config_version=RANKING_CONFIG_VERSION,
                config_digest=CONFIG_DIGEST,
                config=SEARCH_CONFIG,
                document_count=len(prepared),
                chunk_count=sum(len(item.chunks) for item in prepared),
                captured_at=captured.captured_at,
                indexed_at=now,
            )
            db.add(generation)
            db.flush()
            for item in prepared:
                document_row = _document_row(db, item.document)
                chunk_rows = _chunk_rows(db, document_row, item)
                db.add(
                    KnowledgeGenerationDocument(
                        generation_id=generation.id,
                        document_version_id=document_row.id,
                        search_vector=_document_vector(item.document, item.chunks),
                    )
                )
                db.flush()
                for chunk_row, chunk in chunk_rows:
                    db.add(
                        KnowledgeGenerationChunk(
                            generation_id=generation.id,
                            chunk_id=chunk_row.id,
                            document_version_id=document_row.id,
                            search_vector=_chunk_vector(chunk),
                        )
                    )
            changed = True
        else:
            changed = state.active_generation_id != generation.id
        if created_generation:
            outcome = "indexed"
        elif changed:
            outcome = "reactivated"
        else:
            outcome = "unchanged"
        now = datetime.now(timezone.utc)
        state.active_generation_id = generation.id
        state.current_attempt_id = None
        state.last_success_attempt_id = attempt_id
        state.last_checked_source_digest = captured.source_digest
        state.last_checked_at = now
        state.last_success_at = now
        state.last_outcome = outcome
        state.last_reason = None
        state.updated_at = now
        attempt = db.get(KnowledgeIndexAttempt, attempt_id)
        if attempt is not None:
            attempt.phase = "activated"
            attempt.outcome = outcome
            attempt.finished_at = now
            attempt.source_digest = captured.source_digest
            attempt.generation_fingerprint = fingerprint
            attempt.document_count = len(prepared)
            attempt.chunk_count = sum(len(item.chunks) for item in prepared)
            attempt.reason = None
        return KnowledgeIndexRequestResult(
            outcome=outcome,
            generation_id=generation.id,
            attempt_id=attempt_id,
            source_digest=captured.source_digest,
            document_count=len(prepared),
            chunk_count=sum(len(item.chunks) for item in prepared),
        )


def index_corpus(root) -> KnowledgeIndexRequestResult:
    try:
        with WriterGuard() as guard:
            attempt_id = None
            fence = 0
            try:
                # The attempt exists before any source I/O, under the guard.
                attempt_id, fence = _start_attempt(guard.active)
                captured, prepared = _prepare(root)
            except KnowledgeFailure as exc:
                if attempt_id is not None:
                    _fail(guard.active, attempt_id, fence, str(exc))
                return KnowledgeIndexRequestResult(outcome="failed", reason=str(exc))
            except (CorpusError, ChunkingError, ValueError, SQLAlchemyError) as exc:
                reason = _safe_reason(exc)
                if attempt_id is not None:
                    _fail(guard.active, attempt_id, fence, reason)
                return KnowledgeIndexRequestResult(outcome="failed", reason=reason)
            except GuardLost as exc:
                return KnowledgeIndexRequestResult(outcome="failed", reason=_safe_reason(exc))
            try:
                result = _activate(
                    guard.active,
                    guard=guard,
                    captured=captured,
                    prepared=prepared,
                    attempt_id=attempt_id,
                    fence=fence,
                )
                return result
            except (KnowledgeConflict, GuardLost, ValueError, SQLAlchemyError) as exc:
                reason = _safe_reason(exc)
                if isinstance(exc, GuardLost):
                    return KnowledgeIndexRequestResult(
                        outcome="failed", attempt_id=attempt_id, reason=reason
                    )
                _fail(
                    guard.active,
                    attempt_id,
                    fence,
                    reason,
                    source_digest=captured.source_digest,
                )
                return KnowledgeIndexRequestResult(
                    outcome="failed", attempt_id=attempt_id, reason=reason
                )
    except GuardBusy as exc:
        return KnowledgeIndexRequestResult(outcome="index_busy", reason=_safe_reason(exc))
    except (GuardLost, SQLAlchemyError) as exc:
        return KnowledgeIndexRequestResult(outcome="failed", reason=_safe_reason(exc))
