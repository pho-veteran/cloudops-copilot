"""Deterministic, evaluation-only rules comparator over a prepared context."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from cloudops.features.diagnosis.advice import (
    CANDIDATE_BY_FAMILY,
    RCAAdvice,
    UnverifiedCheck,
)
from cloudops.features.diagnosis.context import ContextTarget, PreparedContext
from cloudops.features.diagnosis.versions import FAMILY_REQUIRED
from cloudops.features.evaluation.versions import MANUAL_FAMILIES, RUNBOOK_BY_FAMILY

PRECEDENCE = (
    "image_pull",
    "pending_unschedulable",
    "oom_restarts",
    "crashloop_startup",
    "traffic_latency_errors",
)
OOM_MARKERS = ("oomkilled", "oomkilling", "exit code 137", "exit137")
CRASHLOOP_MARKERS = (
    "crashloopbackoff",
    "back-off restarting",
    "startup failed",
    "missing app_port",
    "bind: address already in use",
    "repeated exits",
)
IMAGE_MARKERS = (
    "imagepullbackoff",
    "errimagepull",
    "manifest unknown",
    "authentication required",
    "unauthorized",
)
PENDING_MARKERS = (
    "insufficient cpu",
    "insufficient memory",
    "untolerated taint",
    "failedscheduling",
)
REQUEST_RISE_RATIO = Decimal("1.5")
ERROR_RATIO_MIN = Decimal("0.05")
P95_SECONDS_MIN = Decimal("0.5")
HEALTHY_REQUIRED = (
    "kubernetes.state",
    "kubernetes.events",
    "kubernetes.logs.current",
    "kubernetes.logs.previous",
    "prometheus.request_rate",
    "prometheus.error_ratio",
    "prometheus.p95_latency",
)
_FIXED_LIMITATIONS = (
    "Deterministic rules comparator; it does not establish semantic quality.",
    "Rules output is advisory and grants no approval, execution or recovery authority.",
)


def _strings(value: Any) -> Iterable[str]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current.lower()
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, (int, float, Decimal)) and not isinstance(current, bool):
            yield str(current).lower()


def _targets(context: PreparedContext, slot: str | None = None) -> list[ContextTarget]:
    values = [*context.required_targets, *context.optional_targets]
    if slot is not None:
        values = [target for target in values if target.source_key == slot]
    return sorted(values, key=lambda target: target.alias)


def _contains(target: ContextTarget, markers: tuple[str, ...]) -> bool:
    text = " ".join([target.label.lower(), *_strings(target.value)])
    return any(marker in text for marker in markers)


def _slot_contains(
    context: PreparedContext, slot: str, markers: tuple[str, ...]
) -> list[ContextTarget]:
    return [target for target in _targets(context, slot) if _contains(target, markers)]


def _has_family(context: PreparedContext, family: str) -> bool:
    return any(item.family == family and item.eligible_for_assessment for item in context.families)


def _number(target: ContextTarget) -> Decimal | None:
    value = target.value
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _sample_time(target: ContextTarget) -> Decimal | None:
    try:
        identity = json.loads(target.identity)
    except (TypeError, ValueError):
        return None
    if not isinstance(identity, list) or len(identity) != 2:
        return None
    try:
        value = Decimal(str(identity[1]))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _samples(context: PreparedContext, slot: str) -> list[tuple[Decimal, Decimal, ContextTarget]]:
    values = []
    for target in _targets(context, slot):
        if target.locator.kind != "sample":
            continue
        timestamp = _sample_time(target)
        number = _number(target)
        if timestamp is not None and number is not None:
            values.append((timestamp, number, target))
    return sorted(values, key=lambda item: (item[0], item[2].alias))


def _last_sample(context: PreparedContext, slot: str) -> tuple[Decimal, str, Decimal] | None:
    samples = _samples(context, slot)
    if not samples:
        return None
    timestamp, value, target = samples[-1]
    return value, target.alias, timestamp


def _traffic_signature(context: PreparedContext) -> bool:
    rates = _samples(context, "prometheus.request_rate")
    errors = _last_sample(context, "prometheus.error_ratio")
    p95 = _last_sample(context, "prometheus.p95_latency")
    if len(rates) < 2 or errors is None or p95 is None:
        return False
    first = rates[0][1]
    last = rates[-1][1]
    if first <= 0:
        return False
    return (
        last / first >= REQUEST_RISE_RATIO
        and errors[0] >= ERROR_RATIO_MIN
        and p95[0] >= P95_SECONDS_MIN
    )


def _matches(context: PreparedContext, family: str) -> bool:
    if not _has_family(context, family):
        return False
    state = _targets(context, "kubernetes.state")
    events = _targets(context, "kubernetes.events")
    logs = [
        *_targets(context, "kubernetes.logs.current"),
        *_targets(context, "kubernetes.logs.previous"),
    ]
    if family == "image_pull":
        return any(_contains(target, IMAGE_MARKERS) for target in state) or any(
            _contains(target, IMAGE_MARKERS) for target in events
        )
    if family == "pending_unschedulable":
        pending = any(
            any(text == "pending" for text in _strings(target.value))
            or ("scheduled" in target.label.lower() and target.value is False)
            for target in state
        )
        return pending and any(_contains(target, PENDING_MARKERS) for target in events)
    if family == "oom_restarts":
        return any(
            _contains(target, OOM_MARKERS)
            or (target.label.lower().endswith("exit_code") and _number(target) == 137)
            for target in [*state, *events, *logs]
        )
    if family == "crashloop_startup":
        return any(_contains(target, CRASHLOOP_MARKERS) for target in [*state, *events, *logs])
    if family == "traffic_latency_errors":
        return _traffic_signature(context)
    return False


def _missing_slots(context: PreparedContext, required: tuple[str, ...]) -> list[str]:
    coverage = {item.slot: item for item in context.source_coverage}
    return [
        slot
        for slot in required
        if slot not in coverage
        or coverage[slot].status != "available"
        or coverage[slot].content_status != "available"
    ]


def _family_support(context: PreparedContext, family: str) -> list[str]:
    state = _targets(context, "kubernetes.state")
    events = _targets(context, "kubernetes.events")
    current = _targets(context, "kubernetes.logs.current")
    previous = _targets(context, "kubernetes.logs.previous")
    picked: list[ContextTarget] = []
    if family == "image_pull":
        picked.extend(target for target in state if _contains(target, IMAGE_MARKERS))
        picked.extend(target for target in events if _contains(target, IMAGE_MARKERS))
    elif family == "pending_unschedulable":
        picked.extend(
            target
            for target in state
            if any(text == "pending" for text in _strings(target.value))
            or ("scheduled" in target.label.lower() and target.value is False)
        )
        picked.extend(target for target in events if _contains(target, PENDING_MARKERS))
    elif family == "oom_restarts":
        picked.extend(
            target
            for target in state
            if _contains(target, OOM_MARKERS)
            or (target.label.lower().endswith("exit_code") and _number(target) == 137)
        )
        picked.extend(target for target in events if _contains(target, OOM_MARKERS))
        picked.extend(current)
        picked.extend(previous)
        for slot in (
            "prometheus.memory_working_set",
            "prometheus.memory_limit",
            "prometheus.restarts",
        ):
            sample = _last_sample(context, slot)
            if sample:
                picked.extend(
                    target for target in _targets(context, slot) if target.alias == sample[1]
                )
    elif family == "crashloop_startup":
        picked.extend(target for target in state if _contains(target, CRASHLOOP_MARKERS))
        picked.extend(target for target in events if _contains(target, CRASHLOOP_MARKERS))
        picked.extend(current)
        picked.extend(previous)
    elif family == "traffic_latency_errors":
        for slot in (
            "prometheus.request_rate",
            "prometheus.error_ratio",
            "prometheus.p95_latency",
        ):
            sample = _last_sample(context, slot)
            if sample:
                picked.extend(
                    target for target in _targets(context, slot) if target.alias == sample[1]
                )
    unique = {target.alias: target for target in picked}
    return sorted(unique, key=lambda alias: alias)[:7]


def _healthy_support(context: PreparedContext) -> list[str]:
    picked: list[ContextTarget] = []
    for slot in ("kubernetes.state", "kubernetes.events"):
        candidates = _targets(context, slot)
        if candidates:
            picked.append(candidates[0])
    for slot in (
        "prometheus.request_rate",
        "prometheus.error_ratio",
        "prometheus.p95_latency",
    ):
        sample = _last_sample(context, slot)
        if sample:
            picked.extend(target for target in _targets(context, slot) if target.alias == sample[1])
    return [target.alias for target in sorted(picked, key=lambda item: item.alias)[:5]]


def _runbooks(context: PreparedContext, family: str | None) -> list[str]:
    if family is None:
        return []
    document_key = RUNBOOK_BY_FAMILY[family]
    candidates = sorted(
        (item for item in context.runbooks if item.reference.document_key == document_key),
        key=lambda item: int(item.alias[1:]),
    )
    return [candidates[0].alias] if candidates else []


def _runbook_limitation(context: PreparedContext, family: str | None) -> list[str]:
    if family is None or _runbooks(context, family):
        return []
    return ["runbook_not_selected_or_not_relevant"]


def _unverified(family: str | None) -> list[UnverifiedCheck]:
    labels = {
        "image_pull": (
            "Registry access",
            "Confirm image reference and registry credentials manually.",
        ),
        "pending_unschedulable": (
            "Scheduling capacity",
            "Confirm requests, taints and available capacity manually.",
        ),
        "oom_restarts": (
            "Memory change",
            "Confirm a bounded profile and resource headroom before any change.",
        ),
        "crashloop_startup": (
            "Rollout history",
            "Review the current and previous rollout before any change.",
        ),
        "traffic_latency_errors": (
            "Traffic cause",
            "Confirm the service bottleneck and capacity before any change.",
        ),
        None: (
            "Fault signature",
            "No supported fault signature is present in the captured evidence.",
        ),
    }
    check, reason = labels[family]
    return [UnverifiedCheck(check=check, reason=reason)]


def compare(context: PreparedContext) -> RCAAdvice:
    matches = [family for family in PRECEDENCE if _matches(context, family)]
    family: str | None
    conflict = False
    if len(matches) == 1:
        family = matches[0]
    elif set(matches) == {"oom_restarts", "crashloop_startup"}:
        family = "oom_restarts"
    elif matches:
        family = None
        conflict = True
    else:
        family = None

    if family is None:
        missing = _missing_slots(context, HEALTHY_REQUIRED)
        summary = (
            "Conflicting fault signatures; manual review required."
            if conflict
            else "The captured evidence does not support a specific fault category."
        )
        next_steps = (
            ["Compare the conflicting observations and verify workload identity manually."]
            if conflict
            else ["Review current workload state and collect the missing or next useful evidence."]
        )
        return RCAAdvice(
            schema_version="RCAAdvice/v1",
            status="insufficient_evidence",
            cause_category="unknown",
            summary=summary,
            supporting_evidence_ids=_healthy_support(context),
            contradicting_evidence_ids=[],
            alternatives=[],
            ruled_out_hypotheses=[],
            unverified_checks=_unverified(None),
            missing_evidence=missing[:20],
            limitations=[*_FIXED_LIMITATIONS, *_runbook_limitation(context, None)],
            next_steps=next_steps,
            runbook_citation_ids=[],
            candidate_template_id=None,
        )

    candidate = None if family in MANUAL_FAMILIES else CANDIDATE_BY_FAMILY[family]
    runbooks = _runbooks(context, family)
    next_steps = {
        "image_pull": ["Check the image reference and registry access; keep remediation manual."],
        "pending_unschedulable": ["Review resource requests, node capacity and taints manually."],
        "oom_restarts": [
            "Compare memory use with its limit and review a bounded profile manually."
        ],
        "crashloop_startup": [
            "Review startup configuration and the previous container logs manually."
        ],
        "traffic_latency_errors": [
            "Compare traffic, error ratio and latency; confirm causality manually."
        ],
    }[family]
    return RCAAdvice(
        schema_version="RCAAdvice/v1",
        status="supported",
        cause_category=family,
        summary={
            "image_pull": "Image pull evidence shows a registry or image reference failure.",
            "pending_unschedulable": "Scheduling evidence shows the workload cannot be placed.",
            "oom_restarts": "Workload evidence shows memory termination or exit code 137.",
            "crashloop_startup": "Workload evidence shows repeated startup failure.",
            "traffic_latency_errors": "Traffic, error ratio and latency rise together; causality is unproven.",
        }[family],
        supporting_evidence_ids=_family_support(context, family),
        contradicting_evidence_ids=[],
        alternatives=[],
        ruled_out_hypotheses=[],
        unverified_checks=_unverified(family),
        missing_evidence=_missing_slots(context, FAMILY_REQUIRED[family])[:20],
        limitations=[*_FIXED_LIMITATIONS, *_runbook_limitation(context, family)],
        next_steps=next_steps,
        runbook_citation_ids=runbooks,
        candidate_template_id=candidate,
    )
