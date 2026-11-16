"""Operator-qualified, exact-target observation policy; absence never enables success."""

import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloudops.features.investigations.locators import digest
from cloudops.features.investigations.prometheus_config import QuerySpec, load_config
from cloudops.features.workloads.baseline_schemas import RestorableContainer
from cloudops.features.workloads.observation_reader import reader_binding
from cloudops.shared.errors import DomainError

VERIFICATION_JOB = "recovery_verification_v1"
CAPABILITY = "conditional_dispatch_v2"
QUERY_IDS = {
    "request_rate",
    "error_ratio",
    "p95_latency",
    "requests",
    "sample_time",
    "resets",
    "offered_rate",
    "offered_sample_time",
}
Template = Literal["scale_deployment", "restore_deployment_template", "set_memory_profile"]


class VerificationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal["RecoveryVerificationProfile/v1"]
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    workload_uid: str = Field(min_length=1, max_length=253)
    template: Template
    containers: list[RestorableContainer] = Field(min_length=1, max_length=32)
    image_ids: dict[str, str]
    replicas: list[Annotated[int, Field(strict=True, ge=1, le=3)]] = Field(
        min_length=1, max_length=3
    )
    source_version: str = Field(min_length=1, max_length=128)
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    qualification_reference: str = Field(min_length=1, max_length=512)
    window_seconds: Literal[180, 300]
    warmup_seconds: Literal[60] = 60
    event_failure_window: Literal["after_settling/v1"] = "after_settling/v1"
    step_seconds: Literal[30] = 30
    sample_lateness_seconds: Literal[10] = 10
    source_age_seconds: Literal[10] = 10
    minimum_coverage: float = Field(ge=0.9, le=1)
    minimum_rps: float = Field(gt=0, le=1000000)
    minimum_load_ratio: float = Field(ge=0.9, le=1)
    maximum_error_ratio: float = Field(ge=0, lt=1)
    maximum_p95_seconds: float = Field(gt=0, le=60)
    queries: dict[str, QuerySpec]

    @model_validator(mode="after")
    def complete(self):
        if self.window_seconds != (180 if self.template == "scale_deployment" else 300):
            raise ValueError("The template requires its qualified observation window.")
        if set(self.queries) != QUERY_IDS:
            raise ValueError("Every required recovery query must be explicitly qualified.")
        if len({c.name for c in self.containers}) != len(self.containers):
            raise ValueError("Qualified containers must be unique.")
        if set(self.image_ids) != {c.name for c in self.containers} or any(
            not value.startswith("sha256:")
            or len(value) != 71
            or any(char not in "0123456789abcdef" for char in value[7:])
            for value in self.image_ids.values()
        ):
            raise ValueError("Each qualified container needs its immutable runtime image digest.")
        if len(set(self.replicas)) != len(self.replicas) or any(
            type(value) is not int or not 1 <= value <= 3 for value in self.replicas
        ):
            raise ValueError("Qualified absolute replicas must be unique and within 1..3.")
        return self

    def digest(self):
        return digest(self.model_dump(mode="json"))

    def matches(self, uid, template, desired):
        return (
            self.workload_uid == uid
            and self.template == template
            and desired.get("uid") == uid
            and desired.get("replicas") in self.replicas
            and desired.get("containers")
            == sorted(
                [c.model_dump(mode="json", exclude_none=True) for c in self.containers],
                key=lambda value: value["name"],
            )
        )


def source_digest():
    config = load_config(os.environ["PROMETHEUS_READ_CONFIG"])
    binding = reader_binding()
    if not binding or config.step_seconds != 30:
        raise ValueError("Qualified read-only source identity is unavailable.")
    return digest(
        {
            "kubernetes_reader": binding,
            "endpoint_id": config.endpoint_id,
            "producer_version": config.producer_version,
            "server": config.server,
            "ca_data": config.ca_data,
            "binding": config.binding.model_dump(),
            "step_seconds": config.step_seconds,
        }
    )


def configured_profiles():
    try:
        with Path(os.environ["RECOVERY_VERIFICATION_FILE"]).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return []
        values = json.loads(raw)
        if not isinstance(values, list) or not 1 <= len(values) <= 128:
            return []
        profiles = [VerificationProfile.model_validate(value) for value in values]
        current_source = source_digest()
        return [profile for profile in profiles if profile.source_digest == current_source]
    except (OSError, KeyError, ValueError, TypeError):
        return []


def load_for(uid, template, desired):
    matches = [
        profile for profile in configured_profiles() if profile.matches(uid, template, desired)
    ]
    return matches[0] if len(matches) == 1 else None


def require_plan(content):
    from cloudops.features.recovery import policy

    profile = load_for(
        content.get("workload", {}).get("uid"), content.get("template"), content.get("desired", {})
    )
    rules = policy.load()
    if (
        content.get("schema_version") != "RecoveryPlan/v2"
        or content.get("execution_capability") != CAPABILITY
        or content.get("direction") not in {"forward", "inverse"}
        or profile is None
        or content.get("verification_profile") != profile.model_dump(mode="json")
        or content.get("verification_profile_digest") != profile.digest()
        or rules is None
        or rules.verification_policy != profile.id
        or content.get("policy")
        != {"id": rules.id, "version": rules.version, "digest": rules.digest()}
    ):
        raise DomainError(
            409,
            "execution_unavailable",
            "This plan lacks the current exact qualified verification profile. Prepare a new plan.",
        )
    return profile
