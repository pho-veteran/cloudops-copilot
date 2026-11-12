"""Explicit manual guidance creation and protected exact Run-linked reads."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.knowledge.guidance import (
    pin_guidance,
    read_guidance,
    resolve_selection_chunk,
)
from cloudops.features.knowledge.guidance_schemas import (
    ManualGuidanceRequest,
    RunGuidance,
    KnowledgeReferenceTarget,
)
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(
    prefix="/api/incidents/{incident_id}/runs/{run_id}/knowledge",
    tags=["knowledge"],
    responses=ERROR_RESPONSES,
)


@router.get(
    "",
    operation_id="get_run_guidance",
    response_model=RunGuidance,
    dependencies=[Depends(current_session)],
)
def read(incident_id: UUID, run_id: UUID, db: Session = Depends(get_db)):
    return read_guidance(db, incident_id, run_id)


@router.post("", operation_id="create_run_guidance", response_model=RunGuidance)
def create(
    incident_id: UUID,
    run_id: UUID,
    body: ManualGuidanceRequest | None = Body(default=None),
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    result = pin_guidance(
        db, incident_id, run_id, consumer="manual", request_identity="manual", actor_id=actor.id
    )
    db.commit()
    return result


@router.get(
    "/selections/{selection_id}/chunks/{chunk_id}",
    operation_id="resolve_run_guidance",
    response_model=KnowledgeReferenceTarget,
    dependencies=[Depends(current_session)],
)
def resolve(
    incident_id: UUID,
    run_id: UUID,
    selection_id: UUID,
    chunk_id: UUID,
    document_version_id: UUID = Query(...),
    db: Session = Depends(get_db),
):
    return resolve_selection_chunk(
        db, incident_id, run_id, selection_id, chunk_id, document_version_id
    )
