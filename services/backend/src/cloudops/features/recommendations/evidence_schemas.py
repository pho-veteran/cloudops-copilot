"""Sanitized source receipt; eligibility is not a numeric recommendation."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from cloudops.features.recommendations.schemas import (
    Coverage,
    Digest,
    Name,
    Reason,
    ReportIdentity,
    ReportWindow,
    SafeModel,
)


class PodEvidence(SafeModel):
    pod: Name
    uid: Name
    replicaset_uid: Name
    container: Name
    start: datetime
    end: datetime
    lifetime: Literal["current", "completed", "unknown"]
    cpu: Coverage
    memory: Coverage
    restarts: Coverage
    cpu_raw_samples: int = Field(ge=0, le=100_000)
    memory_raw_samples: int = Field(ge=0, le=100_000)
    reasons: list[Reason] = Field(default_factory=list, max_length=16)
    memory_reasons: list[Reason] = Field(default_factory=list, max_length=16)


class ResourceEligibility(SafeModel):
    container: Name
    resource: Literal["cpu", "memory"]
    state: Literal["eligible", "insufficient_data", "excluded", "unavailable"]
    reasons: list[Reason] = Field(max_length=16)


class QualifiedEvidence(SafeModel):
    policy_version: Literal["e08-coverage-v1"] = "e08-coverage-v1"
    identity: ReportIdentity
    window: ReportWindow
    query_digest: Digest
    config_digest: Digest
    history_digest: Digest
    collected_at: datetime
    pods: list[PodEvidence] = Field(max_length=100)
    resources: list[ResourceEligibility] = Field(max_length=64)
    reasons: list[Reason] = Field(default_factory=list, max_length=16)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Five-second metric scraping and thirty-second ownership polling are sampled evidence; unobserved events remain possible.",
            "Eligibility alone supplies no KRR recommendation and authorizes no cluster write.",
        ],
        max_length=8,
    )
