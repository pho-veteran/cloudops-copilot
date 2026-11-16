"""Trusted recovery configuration. No deployment-specific values are guessed."""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloudops.features.investigations.locators import digest
from cloudops.features.workloads.kubernetes import quantity
from cloudops.shared.errors import DomainError
from cloudops.features.recovery.fields import safe_resources
from cloudops.features.workloads.baseline_schemas import RestorableContainer


class InverseQualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workload_uid: str = Field(min_length=1, max_length=253)
    containers: list[RestorableContainer] = Field(min_length=1, max_length=32)
    qualification_reference: str = Field(min_length=1, max_length=512)


class MemoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    workload_uid: str = Field(min_length=1, max_length=253)
    containers: list[str] = Field(min_length=1, max_length=32)
    request: str
    limit: str
    qualification_reference: str = Field(min_length=1, max_length=512)


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["RecoveryPolicy/v1"]
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    workload_uids: list[str] = Field(min_length=1, max_length=128)
    max_cpu: str
    max_memory: str
    qualified_images: list[str] = Field(max_length=128)
    memory_profiles: list[MemoryProfile] = Field(max_length=128)
    plan_ttl_seconds: Literal[300] = 300
    review_ttl_seconds: Literal[60] = 60
    field_policy: Literal["regular-container-image-cpu-memory/v1"] = (
        "regular-container-image-cpu-memory/v1"
    )
    scale_bounds: Literal["replicas-1-3/v1"] = "replicas-1-3/v1"
    absence_policy: Literal["require-present-positive/v1"] = "require-present-positive/v1"
    verification_policy: str = Field(min_length=1, max_length=128)
    inverse_policy: str = Field(min_length=1, max_length=128)
    inverse_qualified_states: list[InverseQualification] = Field(
        default_factory=list, max_length=128
    )

    @model_validator(mode="after")
    def safe_values(self):
        if quantity(self.max_cpu) <= 0 or quantity(self.max_memory) <= 0:
            raise ValueError("Positive recovery resource bounds are required.")
        if len({p.id for p in self.memory_profiles}) != len(self.memory_profiles):
            raise ValueError("Memory profile IDs must be unique.")
        for profile in self.memory_profiles:
            if (
                profile.workload_uid not in self.workload_uids
                or len(set(profile.containers)) != len(profile.containers)
                or not all(profile.containers)
                or not 0
                < quantity(profile.request)
                <= quantity(profile.limit)
                <= quantity(self.max_memory)
            ):
                raise ValueError("Memory profile exceeds its qualified scope or bounds.")
        for state in self.inverse_qualified_states:
            if state.workload_uid not in self.workload_uids or len(
                {c.name for c in state.containers}
            ) != len(state.containers):
                raise ValueError(
                    "Inverse qualification must name a unique configured container set and original UID."
                )
            safe_resources(
                [c.model_dump(mode="json", exclude_none=True) for c in state.containers], self
            )
        return self

    def digest(self):
        return digest(self.model_dump(mode="json"))


def load():
    path = os.environ.get("RECOVERY_POLICY_FILE")
    if not path:
        return None
    try:
        with Path(path).open("rb") as stream:
            value = stream.read(65537)
        if len(value) > 65536:
            return None
        return RecoveryPolicy.model_validate(json.loads(value))
    except (OSError, ValueError, ArithmeticError, DomainError):
        return None
