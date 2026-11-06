"""Authenticated read-only comparison and citation routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from cloudops.features.access.dependencies import current_session
from cloudops.features.investigations import comparison_service as service
from cloudops.features.investigations.citation_schemas import (
    CitationResolution,
    CitationTargets,
    LocatorKind,
)
from cloudops.features.investigations.comparison_schemas import RunComparison, DifferenceStatus
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError, ERROR_RESPONSES, ErrorEnvelope

router = APIRouter(
    prefix="/api/incidents/{incident_id}/runs/{run_id}",
    tags=["investigations"],
    dependencies=[Depends(current_session)],
    responses={**ERROR_RESPONSES, 410: {"model": ErrorEnvelope}, 413: {"model": ErrorEnvelope}},
)


def read_snapshot():
    # Separate from the session-authentication transaction: repeatable metadata
    # and content reads in one request, without changing app-wide isolation.
    with sessions()() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        db.execute(text("SET TRANSACTION READ ONLY"))
        yield db


def bounded_response(value):
    if len(value.model_dump_json().encode("utf-8")) > 256 * 1024:
        raise DomainError(
            413,
            "comparison_response_limit",
            "Stored comparison metadata exceeds the response limit. Inspect sources individually.",
        )
    return value


@router.get("/compare", operation_id="compare_runs", response_model=RunComparison)
def compare_runs(
    incident_id: UUID,
    run_id: UUID,
    right_incident_id: UUID,
    right_run_id: UUID,
    source_key: str | None = Query(None, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$"),
    catalog_version: str | None = Query(None, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$"),
    changed_only: bool = False,
    difference_status: DifferenceStatus | None = None,
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=100),
    db=Depends(read_snapshot),
):
    if (source_key is None) != (catalog_version is None):
        raise DomainError(
            422, "invalid_source_selection", "Source and catalog must be selected together."
        )
    return bounded_response(
        service.compare(
            db,
            incident_id,
            run_id,
            right_incident_id,
            right_run_id,
            source_key,
            catalog_version,
            changed_only,
            cursor,
            limit,
            difference_status=difference_status,
        )
    )


@router.get(
    "/evidence/{evidence_id}/citation",
    operation_id="resolve_citation",
    response_model=CitationResolution,
)
def resolve_citation(
    incident_id: UUID,
    run_id: UUID,
    evidence_id: UUID,
    locator_kind: LocatorKind | None = None,
    locator_key: str | None = Query(None, max_length=67, pattern=r"^v1_[a-f0-9]{64}$"),
    db=Depends(read_snapshot),
):
    if (locator_kind is None) != (locator_key is None):
        raise DomainError(422, "invalid_locator", "Locator kind and key must be selected together.")
    return bounded_response(
        service.citation(db, incident_id, run_id, evidence_id, locator_kind, locator_key)
    )


@router.get(
    "/evidence/{evidence_id}/citation-targets",
    operation_id="list_citation_targets",
    response_model=CitationTargets,
)
def list_citation_targets(
    incident_id: UUID,
    run_id: UUID,
    evidence_id: UUID,
    kind: LocatorKind | None = None,
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=100),
    db=Depends(read_snapshot),
):
    return bounded_response(
        service.targets(db, incident_id, run_id, evidence_id, kind, cursor, limit)
    )
