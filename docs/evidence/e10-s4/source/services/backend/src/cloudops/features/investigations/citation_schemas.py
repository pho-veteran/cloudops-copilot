"""Read-only citation projections over already retained evidence."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cloudops.features.investigations.schemas import EvidenceView

LocatorKind = Literal["field", "event", "log_segment", "series", "sample"]


class Locator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: LocatorKind
    key: str = Field(pattern=r"^v1_[a-f0-9]{64}$", max_length=67)


class CitationRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    evidence_id: UUID
    locator: Locator | None = None


class CitationTarget(BaseModel):
    locator: Locator
    label: str
    value: JsonValue
    preview_truncated: bool = False
    multiplicity: int = 1
    ambiguous: bool = False


class CitationResolution(BaseModel):
    schema_version: Literal["CitationResolution/v1"] = "CitationResolution/v1"
    algorithm: Literal["locator_v1"] = "locator_v1"
    citation: CitationRef
    evidence: EvidenceView
    target: CitationTarget | None = None


class CitationTargets(BaseModel):
    algorithm: Literal["locator_v1"] = "locator_v1"
    evidence: EvidenceView
    items: list[CitationTarget]
    next_cursor: str | None = None
