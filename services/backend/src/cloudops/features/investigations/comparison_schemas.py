"""Versioned, read-only comparison transport."""

from typing import Literal
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, JsonValue

from cloudops.features.investigations.citation_schemas import CitationRef
from cloudops.features.investigations.schemas import EvidenceView, RequestWindow
from cloudops.features.diagnosis.api_schemas import AnalysisView

DifferenceStatus = Literal[
    "added", "removed", "changed", "unchanged", "unavailable", "not_comparable"
]


class ComparisonSide(BaseModel):
    incident_id: UUID
    run_id: UUID
    workload_id: UUID
    workload_uid: str
    cluster: str
    namespace: str
    name: str
    state: str
    window: RequestWindow | None
    question: str | None = None
    question_source: str = "not_captured"
    outcome_code: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    analysis: AnalysisView | None = None


class SourceDifference(BaseModel):
    source_key: str
    catalog_version: str
    left: EvidenceView | None
    right: EvidenceView | None
    status: DifferenceStatus
    reason: str | None = None


class SemanticDifference(BaseModel):
    key: str
    kind: str
    label: str
    status: DifferenceStatus
    reason: str | None = None
    left: JsonValue = None
    right: JsonValue = None
    left_citation: CitationRef | None = None
    right_citation: CitationRef | None = None
    preview_truncated: bool = False
    left_occurrences: int | None = None
    right_occurrences: int | None = None


class RunComparison(BaseModel):
    schema_version: Literal["Compare/v1"] = "Compare/v1"
    algorithm: Literal["semantic_v1"] = "semantic_v1"
    left: ComparisonSide
    right: ComparisonSide
    identity_relation: Literal["same_identity", "replacement"]
    sources: list[SourceDifference]
    items: list[SemanticDifference] = []
    next_cursor: str | None = None
    reason: str | None = None
    status: Literal["comparable", "not_comparable"] = "comparable"
