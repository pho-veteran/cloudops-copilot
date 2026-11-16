"""Bound candidate demand including existing/terminating allocations and new rollout Pods."""

from copy import deepcopy

from cloudops.shared.errors import DomainError


def surge(deployment, replicas):
    strategy = deployment["spec"].get("strategy", {})
    if strategy.get("type", "RollingUpdate") == "Recreate":
        return 0
    if strategy.get("type", "RollingUpdate") != "RollingUpdate":
        raise DomainError(409, "capacity_unproven", "Unsupported rollout strategy.")
    value = strategy.get("rollingUpdate", {}).get("maxSurge", "25%")
    if type(value) is int and 0 <= value <= 100:
        return value
    if (
        isinstance(value, str)
        and value.endswith("%")
        and value[:-1].isdigit()
        and 0 <= int(value[:-1]) <= 100
    ):
        return (replicas * int(value[:-1]) + 99) // 100
    raise DomainError(409, "capacity_unproven", "Rollout surge is outside the supported bound.")


def candidate_capacity(kube, deployment, body):
    candidate = deepcopy(deployment)
    desired = body.desired.selected()
    by_name = {c["name"]: c for c in desired["containers"]}
    for container in candidate["spec"]["template"]["spec"]["containers"]:
        selected = by_name[container["name"]]
        container["image"] = selected["image"]
        for group in ("requests", "limits"):
            for key, value in selected["resources"][group].items():
                container.setdefault("resources", {}).setdefault(group, {})[key] = value
    replicas = desired["replicas"]
    candidate["spec"]["replicas"] = replicas
    extra = replicas + (surge(deployment, replicas) if body.template != "scale_deployment" else 0)
    # Retain all existing allocations: terminating old Pods may overlap the full candidate rollout.
    kube.capacity_budget(candidate, extra)
