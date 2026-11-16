"""Closed gateway intent and server-built selected-field patches."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cloudops.features.workloads.baseline_schemas import RestorableContainer


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyIdentity(Closed):
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SelectedState(Closed):
    uid: str = Field(min_length=1, max_length=100)
    replicas: int = Field(strict=True, ge=0, le=10000)
    containers: list[RestorableContainer] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_names(self):
        if len({c.name for c in self.containers}) != len(self.containers):
            raise ValueError("Selected container names must be unique.")
        return self

    def selected(self):
        value = self.model_dump(mode="json", exclude_none=True)
        value["containers"].sort(key=lambda c: c["name"])
        return value


class RestoreSelector(Closed):
    baseline_id: UUID
    baseline_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_profile: Literal["deployment-images-resources/v1"]


class MemorySelector(Closed):
    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: str = Field(min_length=1, max_length=128)
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ScaleSelector(Closed):
    replicas: int = Field(strict=True, ge=1, le=3)


class InverseSelector(Closed):
    forward_operation_id: UUID
    forward_plan_id: UUID


class RecoveryDispatch(Closed):
    schema_version: Literal["RecoveryDispatch/v2"]
    operation_id: UUID
    execution_id: UUID
    direction: Literal["forward", "inverse"]
    template: Literal["restore_deployment_template", "set_memory_profile", "scale_deployment"]
    plan_id: UUID
    plan_version: int = Field(strict=True, ge=1)
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    uid: str = Field(min_length=1, max_length=100)
    epoch: str = Field(min_length=1, max_length=64)
    resource_version: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    policy: PolicyIdentity
    before: SelectedState
    desired: SelectedState
    selector: RestoreSelector | MemorySelector | ScaleSelector | InverseSelector

    @model_validator(mode="after")
    def exact_shape(self):
        expected = (
            InverseSelector
            if self.direction == "inverse"
            else {
                "restore_deployment_template": RestoreSelector,
                "set_memory_profile": MemorySelector,
                "scale_deployment": ScaleSelector,
            }[self.template]
        )
        if not isinstance(self.selector, expected):
            raise ValueError("The selector must match the exact operation direction and template.")
        if (
            self.execution_id != self.operation_id
            or self.before.uid != self.uid
            or self.desired.uid != self.uid
        ):
            raise ValueError(
                "Stable operation/execution and original target identities must match."
            )
        if {c.name for c in self.before.containers} != {c.name for c in self.desired.containers}:
            raise ValueError("Container sets cannot change.")
        return self


class RecoveryReceipt(Closed):
    schema_version: Literal["RecoveryReceipt/v2"] = "RecoveryReceipt/v2"
    operation_id: UUID
    plan_id: UUID
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    attribution: Literal["not_sent", "acknowledged", "denied", "outcome_unknown"]
    code: str = Field(min_length=1, max_length=64)
    reason: str = Field(max_length=2000)
    acknowledged_resource_version: str | None = None
    acknowledged_at: AwareDatetime | None = None


def conditional_patch(body, deployment):
    before, desired = body.before.selected(), body.desired.selected()
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": body.uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": body.resource_version},
    ]
    if before["replicas"] != desired["replicas"]:
        patch.append({"op": "replace", "path": "/spec/replicas", "value": desired["replicas"]})
    old = {c["name"]: c for c in before["containers"]}
    new = {c["name"]: c for c in desired["containers"]}
    for index, actual in enumerate(deployment["spec"]["template"]["spec"]["containers"]):
        name = actual["name"]
        prefix = f"/spec/template/spec/containers/{index}"
        if old[name]["image"] != new[name]["image"]:
            patch.append({"op": "replace", "path": prefix + "/image", "value": new[name]["image"]})
        for group in ("requests", "limits"):
            for key in ("cpu", "memory"):
                if old[name]["resources"][group].get(key) != new[name]["resources"][group].get(key):
                    patch.append(
                        {
                            "op": "replace",
                            "path": f"{prefix}/resources/{group}/{key}",
                            "value": new[name]["resources"][group][key],
                        }
                    )
    return patch
