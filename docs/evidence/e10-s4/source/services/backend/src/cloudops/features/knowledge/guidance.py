"""Bounded full-text retrieval and immutable per-consumer Run selections."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from sqlalchemy import String, and_, func, or_, select
from sqlalchemy.orm import Session, defer

from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.knowledge.catalog import read_chunk, read_document
from cloudops.features.knowledge.corpus import FAMILIES, TEMPLATES
from cloudops.features.knowledge.guidance_schemas import (
    GuidanceChunk,
    KnowledgeRef,
    KnowledgeReferenceTarget,
    KnowledgeRetrieval,
    KnowledgeRunScope,
    KnowledgeSelectionView,
    RunGuidance,
)
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeGeneration,
    KnowledgeGenerationChunk,
    KnowledgeIndexState,
    KnowledgeSelection,
    KnowledgeSelectionChunk,
)
from cloudops.features.workloads.models import Workload
from cloudops.shared.errors import DomainError

CONTENT_BUDGET = 32 * 1024


def _run(db: Session, incident_id: UUID, run_id: UUID, *, lock=False) -> InvestigationRun:
    statement = select(InvestigationRun).where(
        InvestigationRun.id == run_id, InvestigationRun.incident_id == incident_id
    )
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise DomainError(404, "run_not_found", "Investigation Run not found for this incident.")
    return row


def _scope(db, run, family, template) -> KnowledgeRunScope | None:
    if (family is not None and family not in FAMILIES) or (
        template is not None and template not in TEMPLATES
    ):
        raise DomainError(422, "invalid_guidance_scope", "Unsupported guidance applicability.")
    if template is not None and family in {"pending_unschedulable", "image_pull"}:
        raise DomainError(422, "invalid_guidance_scope", "This symptom family is manual-only.")
    incident = db.get(Incident, run.incident_id)
    workload = db.get(Workload, incident.workload_id)
    if (
        not run.question
        or not run.question.strip()
        or run.question_source == "not_captured"
        or run.window_start is None
        or run.window_end is None
        or workload is None
        or not all((workload.uid, workload.cluster, workload.namespace, workload.name))
    ):
        return None
    return KnowledgeRunScope(
        incident_id=incident.id,
        run_id=run.id,
        workload_id=workload.id,
        cluster=workload.cluster,
        namespace=workload.namespace,
        name=workload.name,
        workload_uid=workload.uid,
        window_start=run.window_start,
        window_end=run.window_end,
        symptom_family=family,
        recovery_template=template,
    )


def run_scope(db: Session, incident_id: UUID, run_id: UUID) -> KnowledgeRunScope | None:
    """Public read-only scope for trusted consumers; identical to internal resolution."""
    return _scope(db, _run(db, incident_id, run_id), None, None)


def _retrieve(db, scope, query, limit) -> KnowledgeRetrieval:
    if type(limit) is not int or not 1 <= limit <= 10:
        raise DomainError(422, "invalid_guidance_limit", "Guidance limit must be 1 to 10.")
    if scope is None:
        return KnowledgeRetrieval(status="unavailable", reason="legacy_request", limit=limit)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise DomainError(
            422, "invalid_guidance_query", "Guidance query must be 1 to 2000 characters."
        )
    limitations = ["Methodology only; not telemetry, a diagnosis or execution approval."]
    if scope.symptom_family is None:
        limitations.append(
            "Symptom family applicability is unverified; pack coverage is not a diagnosis."
        )
    if scope.recovery_template:
        limitations.append("Trusted template context does not establish execution eligibility.")
    state = db.get(KnowledgeIndexState, 1)
    generation = (
        db.get(KnowledgeGeneration, state.active_generation_id)
        if state and state.active_generation_id
        else None
    )
    if generation is None:
        return KnowledgeRetrieval(
            status="not_indexed",
            reason="not_indexed",
            scope=scope,
            query=query,
            limit=limit,
            limitations=limitations,
        )
    degraded = bool(
        state.current_attempt_id or state.last_outcome in {"failed", "interrupted", "guard_lost"}
    )
    if degraded:
        limitations.append(
            "Using the retained successful generation during an unfinished or failed index update."
        )
    vector = KnowledgeGenerationChunk.search_vector
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(vector, tsquery)
    kinds = KnowledgeDocumentVersion.guide_kind == "diagnosis"
    if scope.recovery_template:
        kinds = or_(
            kinds,
            and_(
                KnowledgeDocumentVersion.guide_kind == "recovery_template",
                KnowledgeDocumentVersion.applicability["recovery_templates"].contains(
                    [scope.recovery_template]
                ),
            ),
        )
    statement = (
        select(KnowledgeChunk, KnowledgeDocumentVersion, rank)
        .join(KnowledgeGenerationChunk, KnowledgeGenerationChunk.chunk_id == KnowledgeChunk.id)
        .join(
            KnowledgeDocumentVersion,
            KnowledgeDocumentVersion.id == KnowledgeChunk.document_version_id,
        )
        .options(defer(KnowledgeDocumentVersion.content))
        .where(
            KnowledgeGenerationChunk.generation_id == generation.id,
            KnowledgeDocumentVersion.applicability["kind"].astext == scope.kind,
            kinds,
            vector.op("@@")(tsquery),
        )
    )
    if scope.symptom_family:
        families = KnowledgeDocumentVersion.applicability["symptom_families"]
        statement = statement.where(or_(families == [], families.contains([scope.symptom_family])))
    rows = db.execute(
        statement.order_by(
            rank.desc(),
            KnowledgeDocumentVersion.document_key,
            KnowledgeDocumentVersion.version,
            KnowledgeChunk.ordinal,
            KnowledgeChunk.id,
        ).limit(limit + 1)
    ).all()
    chunks = []
    size = 0
    reason = "result_limit" if len(rows) > limit else None
    for chunk, document, score in rows[:limit]:
        if size + len(chunk.content) > CONTENT_BUDGET:
            reason = "content_budget"
            break
        size += len(chunk.content)
        chunks.append(
            GuidanceChunk(
                reference=KnowledgeRef(
                    document_key=document.document_key,
                    document_version_id=document.id,
                    version=document.version,
                    chunk_id=chunk.id,
                    chunker_version=chunk.chunker_version,
                    document_sha256=document.content_sha256,
                    metadata_sha256=document.metadata_sha256,
                    chunk_sha256=chunk.content_sha256,
                ),
                title=document.title,
                heading_path=chunk.heading_path,
                content=chunk.content.decode("utf-8"),
                source_path=document.source_path,
                provenance=document.provenance,
                applicability=document.applicability,
                score=float(score),
            )
        )
    return KnowledgeRetrieval(
        status="partial" if reason else "matched" if chunks else "no_matches",
        reason=reason,
        query=query,
        scope=scope,
        generation_id=generation.id,
        source_digest=generation.source_digest,
        chunker_version=generation.chunker_version,
        ranking_config_version=generation.ranking_config_version,
        search_config_version=generation.search_config_version,
        chunks=chunks,
        content_bytes=size,
        limit=limit,
        truncated=reason is not None,
        degraded=degraded,
        limitations=limitations,
    )


def retrieve(
    db: Session,
    incident_id: UUID,
    run_id: UUID,
    *,
    query: str | None = None,
    limit: int = 10,
    symptom_family: str | None = None,
    recovery_template: str | None = None,
) -> KnowledgeRetrieval:
    """Trusted callers provide query/context; identity always resolves from the persisted Run."""
    run = _run(db, incident_id, run_id)
    scope = _scope(db, run, symptom_family, recovery_template)
    return _retrieve(db, scope, query if query is not None else run.question, limit)


def _view(row: KnowledgeSelection) -> KnowledgeSelectionView:
    return KnowledgeSelectionView(
        id=row.id,
        run_id=row.run_id,
        actor_id=row.actor_id,
        created_at=row.created_at,
        origin="manual" if row.consumer == "manual" else "trusted",
        retrieval=row.payload,
    )


def analysis_selection(
    db: Session, run_id: UUID, selection_id: UUID, request_identity: str
) -> KnowledgeSelectionView | None:
    row = db.scalar(
        select(KnowledgeSelection).where(
            KnowledgeSelection.id == selection_id,
            KnowledgeSelection.run_id == run_id,
            KnowledgeSelection.consumer == "analysis",
            KnowledgeSelection.request_identity == request_identity,
        )
    )
    return _view(row) if row is not None else None


def read_guidance(db: Session, incident_id: UUID, run_id: UUID) -> RunGuidance:
    run = _run(db, incident_id, run_id)
    row = db.scalar(
        select(KnowledgeSelection).where(
            KnowledgeSelection.run_id == run_id,
            KnowledgeSelection.consumer == "manual",
            KnowledgeSelection.request_identity == "manual",
        )
    )
    if row is not None:
        return RunGuidance(state="saved", selection=_view(row))
    if _scope(db, run, None, None) is None:
        return RunGuidance(state="unavailable", reason="legacy_request")
    return RunGuidance(state="not_selected")


def pin_guidance(
    db: Session,
    incident_id: UUID,
    run_id: UUID,
    *,
    consumer: Literal["manual", "analysis"],
    request_identity: str,
    actor_id: UUID | None = None,
    query: str | None = None,
    symptom_family: str | None = None,
    recovery_template: str | None = None,
    limit: int = 10,
) -> RunGuidance:
    """Flush a selection in the caller's transaction; no collection, model or cluster calls."""
    if (
        consumer not in {"manual", "analysis"}
        or not request_identity
        or len(request_identity) > 128
    ):
        raise DomainError(
            422, "invalid_guidance_request", "Invalid trusted consumer request identity."
        )
    if consumer == "manual" and (
        request_identity != "manual"
        or actor_id is None
        or query is not None
        or symptom_family is not None
        or recovery_template is not None
    ):
        raise DomainError(
            422, "invalid_guidance_request", "Manual guidance uses the stored Run request."
        )
    run = _run(db, incident_id, run_id, lock=True)
    row = db.scalar(
        select(KnowledgeSelection).where(
            KnowledgeSelection.run_id == run_id,
            KnowledgeSelection.consumer == consumer,
            KnowledgeSelection.request_identity == request_identity,
        )
    )
    if row is not None:
        return RunGuidance(state="saved", selection=_view(row))
    scope = _scope(db, run, symptom_family, recovery_template)
    result = _retrieve(db, scope, query if query is not None else run.question, limit)
    if result.status in {"not_indexed", "unavailable"}:
        return RunGuidance(state="unavailable", reason=result.reason)
    row = KnowledgeSelection(
        run_id=run_id,
        generation_id=result.generation_id,
        consumer=consumer,
        request_identity=request_identity,
        actor_id=actor_id,
        payload=result.model_dump(mode="json"),
    )
    db.add(row)
    db.flush()
    for ordinal, chunk in enumerate(result.chunks):
        db.add(
            KnowledgeSelectionChunk(
                selection_id=row.id,
                generation_id=result.generation_id,
                ordinal=ordinal,
                chunk_id=chunk.reference.chunk_id,
            )
        )
    db.flush()
    return RunGuidance(state="saved", selection=_view(row))


def resolve_reference(db: Session, reference: KnowledgeRef) -> KnowledgeReferenceTarget:
    document = read_document(db, reference.document_version_id)
    chunk = read_chunk(db, reference.chunk_id, reference.document_version_id)
    if (
        reference.document_key != document.document_key
        or reference.version != document.version
        or reference.chunker_version != chunk.chunker_version
        or reference.document_sha256 != document.content_sha256
        or reference.metadata_sha256 != document.metadata_sha256
        or reference.chunk_sha256 != chunk.content_sha256
        or hashlib.sha256(document.content.encode("utf-8")).hexdigest() != reference.document_sha256
        or hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() != reference.chunk_sha256
    ):
        raise DomainError(
            409,
            "knowledge_reference_mismatch",
            "The exact runbook reference does not match stored content.",
        )
    return KnowledgeReferenceTarget(reference=reference, document=document, chunk=chunk)


def resolve_selection_chunk(
    db: Session,
    incident_id: UUID,
    run_id: UUID,
    selection_id: UUID,
    chunk_id: UUID,
    document_version_id: UUID,
    *,
    allow_evaluation: bool = False,
) -> KnowledgeReferenceTarget:
    _run(db, incident_id, run_id)
    row = db.scalar(
        select(KnowledgeSelection).where(
            KnowledgeSelection.id == selection_id,
            KnowledgeSelection.run_id == run_id,
        )
    )
    if row and row.consumer == "analysis" and not allow_evaluation:
        from cloudops.features.diagnosis.models import Analysis

        evaluation = db.scalar(
            select(Analysis.id).where(
                Analysis.run_id == run_id,
                Analysis.purpose == "evaluation",
                Analysis.id.cast(String) == row.request_identity,
            )
        )
        if evaluation:
            raise DomainError(
                404,
                "knowledge_reference_not_found",
                "Runbook reference is not an operational selection.",
            )
    member = (
        db.scalar(
            select(KnowledgeSelectionChunk).where(
                KnowledgeSelectionChunk.selection_id == selection_id,
                KnowledgeSelectionChunk.chunk_id == chunk_id,
            )
        )
        if row
        else None
    )
    if member is None:
        raise DomainError(
            404,
            "knowledge_reference_not_found",
            "Runbook reference not found in this Run selection.",
        )
    selected = _view(row)
    reference = next(
        (
            item.reference
            for item in selected.retrieval.chunks
            if item.reference.chunk_id == chunk_id
        ),
        None,
    )
    if reference is None or reference.document_version_id != document_version_id:
        raise DomainError(
            404, "knowledge_reference_not_found", "Runbook reference not found for this document."
        )
    target = resolve_reference(db, reference)
    target.selection_id = selection_id
    return target
