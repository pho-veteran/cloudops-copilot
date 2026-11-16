"""Shared recovery-selected fields; never whole templates or configuration contents."""

from cloudops.features.investigations.locators import digest
from cloudops.features.workloads.kubernetes import quantity
from cloudops.features.workloads.observation_evaluator import extract


class Ineligible(ValueError):
    pass


def selected(deployment):
    fields, refs, reasons = extract(deployment)
    if reasons:
        raise Ineligible("Unsupported configuration references.")
    return sorted(
        [
            {
                "name": c.name,
                "image": c.image,
                "resources": {
                    "requests": c.requests.model_dump(exclude_none=True),
                    "limits": c.limits.model_dump(exclude_none=True),
                },
            }
            for c in fields
        ],
        key=lambda c: c["name"],
    ), sorted(
        [ref.model_dump(mode="json", exclude_none=True) for ref in refs],
        key=lambda r: (r["path"], digest(r)),
    )


def safe_resources(containers, rules):
    for container in containers:
        resources = container["resources"]
        for key, maximum in (("cpu", rules.max_cpu), ("memory", rules.max_memory)):
            request, limit = (
                quantity(resources[group].get(key, 0)) for group in ("requests", "limits")
            )
            if not 0 < request <= limit <= quantity(maximum):
                raise Ineligible(
                    "Required resource fields are absent or outside safe policy bounds."
                )
