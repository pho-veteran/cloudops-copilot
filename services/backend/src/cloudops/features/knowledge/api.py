"""Authenticated read-only Knowledge catalog APIs."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.knowledge.catalog import (
    index_status,
    list_documents,
    read_chunk,
    read_document,
)
from cloudops.features.knowledge.schemas import (
    KnowledgeChunkDetail,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeIndexStatus,
)
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(
    prefix="/api/knowledge",
    tags=["knowledge"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(current_session)],
)


@router.get(
    "/documents",
    operation_id="list_knowledge_documents",
    response_model=KnowledgeDocumentPage,
)
def documents(
    q: str | None = Query(default=None, max_length=2000),
    tags: list[str] | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
):
    return list_documents(db, query=q, tags=tags or [], limit=limit, cursor=cursor)


@router.get(
    "/documents/{document_version_id}",
    operation_id="read_knowledge_document",
    response_model=KnowledgeDocumentDetail,
)
def document(document_version_id: UUID, db: Session = Depends(get_db)):
    return read_document(db, document_version_id)


@router.get(
    "/chunks/{chunk_id}",
    operation_id="read_knowledge_chunk",
    response_model=KnowledgeChunkDetail,
)
def chunk(
    chunk_id: UUID,
    document_version_id: UUID = Query(...),
    db: Session = Depends(get_db),
):
    return read_chunk(db, chunk_id, document_version_id)


@router.get(
    "/index-status",
    operation_id="get_knowledge_index_status",
    response_model=KnowledgeIndexStatus,
)
def status(db: Session = Depends(get_db)):
    return index_status(db)
