"""Offline replay of one configuration over RawEvidenceBundle/v1 cases.

B0 is the keyword rules comparator and B1 the current product LLM path, both over the
product context (`context.assemble`) rebuilt from the bundle. Later configurations add
the engine stages A-E. Each case yields one JSON record that the scorer reads without
the bundle or the label.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from engine_loop import bundle as raw
from engine_loop.gateway import Gateway

from cloudops.features.diagnosis.advice import (
    RCAAdvice,
    feedback_message,
    parse,
    validate_structural,
)
from cloudops.features.diagnosis.context import InsufficientContext, PreparedContext, assemble
from cloudops.features.diagnosis.egress import EgressPolicy, check_send
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.evaluation.rules import compare

OVERFLOW_REASONS = {"required_targets_exceed_budget", "request_exceeds_budget", "budget_exceeded"}
DENY_PATTERNS = [
    {"name": "authorization_header", "regex": "(?i)\\bbearer\\s+[A-Za-z0-9._~+/-]{16,}"},
    {"name": "private_key_block", "regex": "-----BEGIN [A-Z ]*PRIVATE KEY-----"},
    {
        "name": "assigned_secret",
        "regex": "(?i)\\b(password|passwd|token|secret|api[_-]?key)\\s*[:=]\\s*\\S{8,}",
    },
]


def policy(*, max_message_bytes: int = 65536, max_target_bytes: int = 8192) -> EgressPolicy:
    return EgressPolicy.model_validate(
        {
            "schema": "EgressPolicy/v1",
            "policy_id": "engine-loop-dev",
            "policy_version": "1",
            "destination": {
                "config_id": "engine-loop-dev",
                "config_version": "1",
                "endpoint_label": "local-proxy",
            },
            "allowed_source_classes": [
                "operator_request",
                "kubernetes_state",
                "kubernetes_events",
                "container_logs",
                "prometheus_series",
                "runbook",
            ],
            "max_message_bytes": max_message_bytes,
            "max_target_bytes": max_target_bytes,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": DENY_PATTERNS,
        }
    )


@dataclass(frozen=True)
class Settings:
    """Experiment knobs recorded in the run config."""

    budget: int = 65536
    repair: bool = True
    variant: dict[str, str] = field(default_factory=dict)


def product_context(
    value: raw.RawEvidenceBundle, model_profile: ModelProfile, egress: EgressPolicy
) -> PreparedContext | InsufficientContext:
    return assemble(
        raw.materials(value),
        raw.scope(value),
        None,
        egress,
        model_profile,
        f"engine-loop.{value.case_id}",
    )


def context_summary(context: PreparedContext | InsufficientContext) -> dict:
    if isinstance(context, InsufficientContext):
        return {
            "outcome": "insufficient",
            "reason": context.reason,
            "overflow": context.reason in OVERFLOW_REASONS,
            "families": _families(context),
            "message_bytes": None,
            "required_targets": 0,
            "optional_targets": 0,
            "sent_slots": [],
            "digest": context.digest,
        }
    targets = [*context.required_targets, *context.optional_targets]
    return {
        "outcome": "prepared",
        "reason": None,
        "overflow": False,
        "families": _families(context),
        "message_bytes": context.message_bytes,
        "system_bytes": len(context.messages[0].content.encode("utf-8")),
        "user_bytes": len(context.messages[1].content.encode("utf-8")),
        "content_bytes": context.content_bytes,
        "required_targets": len(context.required_targets),
        "optional_targets": len(context.optional_targets),
        "sent_slots": sorted({target.source_key for target in targets}),
        "omissions": [item.model_dump(mode="json") for item in context.omissions],
        "aliases": {target.alias: target.source_key for target in targets},
        "runbooks": {item.alias: item.reference.document_key for item in context.runbooks},
        "digest": context.digest,
        "versions": {
            "context": context.context_version,
            "prompt": context.prompt_version,
            "validator": context.validator_version,
        },
    }


def _families(context) -> dict[str, bool]:
    return {item.family: item.eligible_for_assessment for item in context.families}


def advice_summary(advice: RCAAdvice | None) -> dict | None:
    if advice is None:
        return None
    return advice.model_dump(mode="json")


def _abstain_record() -> dict:
    """The product outcome for an insufficient context: no model call, abstention."""
    return {"status": "insufficient_evidence", "cause_category": "unknown", "source": "context"}


async def model_advice(
    context: PreparedContext,
    *,
    case_id: str,
    gateway: Gateway,
    egress: EgressPolicy,
    repair: bool,
) -> tuple[RCAAdvice | None, list[dict]]:
    """One product-shaped attempt, plus one structural repair when allowed."""
    attempts: list[dict] = []
    send = check_send(context, egress, gateway.model_profile, "initial")
    if not send.ok:
        attempts.append({"phase": "initial", "send_failure": send.code})
        return None, attempts
    result = await gateway.call(list(send.messages), case_id=case_id, phase="initial")
    advice, failures, record = _judge(context, result, "initial")
    attempts.append(record)
    if advice is not None and not failures:
        return advice, attempts
    repairable = [item for item in failures if item.repairable]
    if not repair or result["kind"] != "text" or not repairable:
        return None, attempts
    feedback = feedback_message(gateway.model_profile, context, repairable)
    if feedback is None:
        attempts.append({"phase": "repair", "send_failure": "repair_unavailable"})
        return None, attempts
    send = check_send(context, egress, gateway.model_profile, "repair", feedback=feedback)
    if not send.ok:
        attempts.append({"phase": "repair", "send_failure": send.code})
        return None, attempts
    result = await gateway.call(list(send.messages), case_id=case_id, phase="repair")
    advice, failures, record = _judge(context, result, "repair")
    attempts.append(record)
    if advice is not None and not failures:
        return advice, attempts
    return None, attempts


def _judge(context: PreparedContext, result: dict, phase: str):
    record = {
        "phase": phase,
        "request_digest": result["request_digest"],
        "cached": result["cached"],
        "kind": result["kind"],
        "finish_reason": result["finish_reason"],
        "usage": result["usage"],
        "elapsed_ms": result["elapsed_ms"],
    }
    if result["kind"] != "text" or result["text"] is None:
        record["failures"] = ["truncated" if result["kind"] == "truncated" else result["kind"]]
        return None, [], record
    parsed = parse(result["text"])
    if isinstance(parsed, list):
        record["failures"] = sorted({item.code for item in parsed})
        record["advice"] = None
        return None, parsed, record
    failures = validate_structural(context, parsed)
    record["failures"] = sorted({item.code for item in failures})
    record["advice"] = parsed.model_dump(mode="json")
    return parsed, failures, record


async def run_b0(value, *, settings: Settings, gateway: Gateway | None = None) -> dict:
    from engine_loop.gateway import profile

    model_profile = profile(max_input_bytes=settings.budget)
    egress = policy(max_message_bytes=settings.budget)
    context = product_context(value, model_profile, egress)
    summary = context_summary(context)
    if isinstance(context, InsufficientContext):
        return {"context": summary, "attempts": [], "final": _abstain_record()}
    advice = compare(context)
    failures = sorted({item.code for item in validate_structural(context, advice)})
    return {
        "context": summary,
        "attempts": [],
        "final": {
            **advice.model_dump(mode="json"),
            "source": "rules",
            "valid": not failures,
            "failures": failures,
        },
    }


async def run_b1_context(value, *, settings: Settings, gateway: Gateway | None = None) -> dict:
    """Product context only (no model): the zero-call twin of B1 for byte metrics."""
    from engine_loop.gateway import profile

    egress = policy(max_message_bytes=settings.budget)
    context = product_context(value, profile(max_input_bytes=settings.budget), egress)
    return {"context": context_summary(context), "attempts": [], "final": _abstain_record()}


async def run_b1(value, *, settings: Settings, gateway: Gateway) -> dict:
    egress = policy(max_message_bytes=settings.budget)
    context = product_context(value, gateway.model_profile, egress)
    summary = context_summary(context)
    if isinstance(context, InsufficientContext):
        return {"context": summary, "attempts": [], "final": _abstain_record()}
    advice, attempts = await model_advice(
        context, case_id=value.case_id, gateway=gateway, egress=egress, repair=settings.repair
    )
    if advice is None:
        return {"context": summary, "attempts": attempts, "final": {"source": "model_invalid"}}
    return {
        "context": summary,
        "attempts": attempts,
        "final": {**advice.model_dump(mode="json"), "source": "model", "valid": True},
    }


def alias_map(context: PreparedContext) -> dict[tuple[str, str, str], str]:
    return {
        (str(target.evidence_id), target.locator.kind, target.locator.key): target.alias
        for target in (*context.required_targets, *context.optional_targets)
    }


def cite(context: PreparedContext | InsufficientContext, refs, limit: int = 20) -> list[str]:
    if not isinstance(context, PreparedContext):
        return []
    aliases = alias_map(context)
    found = []
    for ref in refs:
        alias = aliases.get((ref.evidence_id, ref.kind, ref.key))
        if alias and alias not in found:
            found.append(alias)
    return sorted(found)[:limit]


EMPTY_ADVICE = {
    "schema_version": "RCAAdvice/v1",
    "supporting_evidence_ids": [],
    "contradicting_evidence_ids": [],
    "alternatives": [],
    "ruled_out_hypotheses": [],
    "unverified_checks": [],
    "missing_evidence": [],
    "limitations": [],
    "next_steps": [],
    "runbook_citation_ids": [],
    "candidate_template_id": None,
}


def detector_advice(detection, context) -> RCAAdvice:
    from engine_loop.lab import CANDIDATE_BY_FAMILY, MANUAL

    family = detection.family
    if family == "unknown":
        return RCAAdvice.model_validate(
            {
                **EMPTY_ADVICE,
                "status": "insufficient_evidence",
                "cause_category": "unknown",
                "summary": f"Detector abstained ({detection.reason}).",
                "next_steps": ["Review the stored workload evidence manually."],
            }
        )
    top = detection.hypotheses[0]
    manual = family in MANUAL
    return RCAAdvice.model_validate(
        {
            **EMPTY_ADVICE,
            "status": "supported",
            "cause_category": family,
            "summary": f"Detector {family} score {top.score:.1f}: "
            + ", ".join(name for name, _ in top.features),
            "supporting_evidence_ids": cite(context, top.support),
            "unverified_checks": (
                [{"check": "Confirm the cause manually", "reason": "Manual-only family."}]
                if manual
                else []
            ),
            "next_steps": ["Review the cited evidence before any change."],
            "candidate_template_id": None if manual else CANDIDATE_BY_FAMILY.get(family),
        }
    )


async def run_b2(value, *, settings: Settings, gateway: Gateway | None = None) -> dict:
    from engine_loop.gateway import profile

    from cloudops.features.diagnosis.context import family_matrix
    from cloudops.features.diagnosis.engine import detector, signals

    model_profile = profile(max_input_bytes=settings.budget)
    egress = policy(max_message_bytes=settings.budget)
    materials = raw.materials(value)
    table = signals.extract(materials)
    detection = detector.detect(table, family_matrix(materials, egress))
    context = product_context(value, model_profile, egress)
    advice = detector_advice(detection, context)
    failures = (
        sorted({item.code for item in validate_structural(context, advice)})
        if isinstance(context, PreparedContext)
        else []
    )
    return {
        "context": context_summary(context),
        "attempts": [],
        "detector": detection.record(),
        "signals": table.records(),
        "final": {
            **advice.model_dump(mode="json"),
            "source": "detector",
            # Detector-only: family scoring does not depend on a sendable context.
            "valid": True,
            "failures": failures,
        },
    }


def engine_context(
    value,
    settings: Settings,
    model_profile,
    egress,
    *,
    with_signals: bool,
    default_prompt: str | None = None,
):
    from cloudops.features.diagnosis.context import family_matrix
    from cloudops.features.diagnosis.engine import detector, prompt, selection, signals

    materials = raw.materials(value)
    table = signals.extract(materials)
    detection = detector.detect(table, family_matrix(materials, egress))
    version = settings.variant.get("prompt", default_prompt or prompt.PROMPT_V1)
    show_signals = with_signals and settings.variant.get("signals", "on") == "on"
    show_hypotheses = with_signals and settings.variant.get("hypotheses", "on") == "on"

    def extra(aliases):
        return prompt.sections(
            table if show_signals else None,
            detection if show_hypotheses else None,
            aliases,
        )

    context = selection.assemble(
        materials,
        raw.scope(value),
        egress,
        model_profile,
        f"engine-loop.{value.case_id}",
        system_prompt=prompt.PROMPTS[version],
        prompt_version=version,
        table=table if settings.variant.get("relevance", "signals") == "signals" else None,
        extra_sections=extra,
        soft_budget=int(settings.variant.get("soft_budget", selection.SOFT_BUDGET_BYTES)),
    )
    return context, table, detection


async def run_b3(value, *, settings: Settings, gateway: Gateway) -> dict:
    """Compact selection and encoding (stage C) with the v1 prompt; no signal table."""
    egress = policy(max_message_bytes=settings.budget)
    context, _, _ = engine_context(
        value, settings, gateway.model_profile, egress, with_signals=False
    )
    summary = context_summary(context)
    if isinstance(context, InsufficientContext):
        return {"context": summary, "attempts": [], "final": _abstain_record()}
    advice, attempts = await model_advice(
        context, case_id=value.case_id, gateway=gateway, egress=egress, repair=settings.repair
    )
    if advice is None:
        return {"context": summary, "attempts": attempts, "final": {"source": "model_invalid"}}
    return {
        "context": summary,
        "attempts": attempts,
        "final": {**advice.model_dump(mode="json"), "source": "model", "valid": True},
    }


async def run_c0(value, *, settings: Settings, gateway: Gateway | None = None) -> dict:
    """Stage C context only (no model): bytes, overflow and required-source recall."""
    from engine_loop.gateway import profile

    egress = policy(max_message_bytes=settings.budget)
    with_signals = settings.variant.get("gate", "b3") == "b4"
    context, _, _ = engine_context(
        value,
        settings,
        profile(max_input_bytes=settings.budget),
        egress,
        with_signals=with_signals,
        default_prompt="engine.rca_prompt/v3" if with_signals else None,
    )
    return {"context": context_summary(context), "attempts": [], "final": _abstain_record()}


async def run_b4(value, *, settings: Settings, gateway: Gateway) -> dict:
    """Full pipeline: signals and hypotheses (A, B), compact context (C), prompt v2 (D)
    and the consistency check (E) on top of the unchanged validator."""
    from cloudops.features.diagnosis.engine import consistency, prompt

    egress = policy(max_message_bytes=settings.budget)
    context, table, detection = engine_context(
        value,
        settings,
        gateway.model_profile,
        egress,
        with_signals=True,
        default_prompt=prompt.PROMPT_V3,
    )
    summary = context_summary(context)
    record = {"context": summary, "detector": detection.record()}
    if isinstance(context, InsufficientContext):
        return {**record, "attempts": [], "final": _abstain_record()}
    advice, attempts = await model_advice(
        context, case_id=value.case_id, gateway=gateway, egress=egress, repair=settings.repair
    )
    if advice is None:
        return {**record, "attempts": attempts, "final": {"source": "model_invalid"}}
    checked, note = consistency.check(
        advice, detection, context, policy=settings.variant.get("consistency", "off")
    )
    return {
        **record,
        "attempts": attempts,
        "consistency": note,
        "final": {**checked.model_dump(mode="json"), "source": "model", "valid": True},
    }


Runner = Callable[..., Any]
CONFIGS: dict[str, tuple[Runner, bool]] = {
    # name -> (runner, needs model)
    "B0": (run_b0, False),
    "B1": (run_b1, True),
    "B2": (run_b2, False),
    "B3": (run_b3, True),
    "B4": (run_b4, True),
    # Zero-call gate for stage C: context metrics only, the answer is a fixed abstention.
    "C0": (run_c0, False),
    "P1": (run_b1_context, False),
}
