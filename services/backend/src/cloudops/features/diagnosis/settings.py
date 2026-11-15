"""Trusted deployment identities; reading status never opens a model connection."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.versions import CONTEXT_VERSION, PROMPT_VERSION, VALIDATOR_VERSION


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["DiagnosisQualification/v1"]
    cohort_id: str = Field(min_length=1, max_length=128)
    evidence_reference: str = Field(min_length=1, max_length=512)
    identity: dict[str, str]


@dataclass(frozen=True)
class Deployment:
    profile: ModelProfile | None
    policy: EgressPolicy | None
    qualification: Qualification | None
    problem: str | None = None

    def identity(self) -> dict[str, str]:
        if not self.profile or not self.policy:
            return {}
        return {
            "config_id": self.profile.config_id,
            "config_version": self.profile.config_version,
            "endpoint_label": self.profile.endpoint_label or "",
            "behavior_digest": self.profile.behavior_digest(),
            "model": self.profile.model or "",
            "policy_digest": self.policy.digest(),
            "prompt_version": PROMPT_VERSION,
            "context_version": CONTEXT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "lifecycle_version": "e06.lifecycle/v1:calls3:repair1:deadline900",
        }

    def denied(self, purpose: str = "operational") -> str | None:
        if self.problem:
            return self.problem
        if self.profile is None or self.policy is None:
            return "not_configured"
        if self.profile.mode == "disabled":
            return "disabled"
        if not self.policy.destination_matches(self.profile.identity()):
            return "destination_mismatch"
        if purpose == "evaluation":
            return None if self.profile.mode == "evaluation_only" else "evaluation_mode_required"
        if self.profile.mode != "operational":
            return "evaluation_only"
        if not self.qualification or self.qualification.identity != self.identity():
            return "unqualified"
        return None


def load() -> Deployment:
    try:
        profile = ModelProfile.load_optional(os.environ.get("DIAGNOSIS_PROFILE_FILE"))
        policy = EgressPolicy.load_optional(os.environ.get("DIAGNOSIS_EGRESS_FILE"))
        qualification = None
        path = os.environ.get("DIAGNOSIS_QUALIFICATION_FILE")
        if path:
            with Path(path).open("rb") as stream:
                payload = stream.read(65537)
            if len(payload) > 65536:
                raise ValueError("Qualification exceeds bound.")
            qualification = Qualification.model_validate(json.loads(payload))
        return Deployment(profile, policy, qualification)
    except (ValueError, OSError):
        return Deployment(None, None, None, "configuration_invalid")
