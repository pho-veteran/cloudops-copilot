"""The engine diagnosis path as a selectable product context profile.

Stages A (signals), B (detector) and C (selection) build the context and stage D's
versioned prompt and hypothesis/signal sections go into it. The product worker and the
offline engine loop (`scripts/engine_loop/pipeline.py`) call `prepare`, so a replayed
bundle and a stored Run with the same evidence produce the same context digest. The
model output still passes the unchanged validator; the model never authorizes an action.
"""

from __future__ import annotations

from dataclasses import dataclass

from cloudops.features.diagnosis.context import (
    ContextScope,
    InsufficientContext,
    KnowledgeSelectionView,
    PreparedContext,
    SlotMaterial,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.advice import RCAAdvice
from cloudops.features.diagnosis.engine import consistency, detector, prompt, selection, signals
from cloudops.features.diagnosis.profile import ModelProfile

PROFILE = "engine"
PROMPT_VERSION = prompt.PROMPT_V3
# Stage E as kept in the loop (exp-0014): support aliases only from required slots.
CONSISTENCY_POLICY = consistency.DEFAULT_POLICY
# Stage identities that are not already carried by the context and prompt versions.
STAGES_VERSION = (
    f"{signals.SIGNALS_VERSION}+{detector.DETECTOR_VERSION}"
    f"+{consistency.CONSISTENCY_VERSION}:{CONSISTENCY_POLICY}"
)


# The trusted runbook query of each family: the title of its E05 diagnosis guide
# (`runbooks/catalog.json`) as one phrase. The title is indexed into every chunk of
# that guide, so the phrase selects that guide's chunks.
RUNBOOK_QUERIES = {
    "crashloop_startup": '"Investigate CrashLoop and startup failures after a change"',
    "oom_restarts": '"Investigate OOM and repeated restarts"',
    "traffic_latency_errors": '"Investigate traffic, latency and error increases"',
    "pending_unschedulable": '"Investigate Pending and unschedulable Pods"',
    "image_pull": '"Investigate image pull failures"',
}


def guidance_query(materials: list[SlotMaterial], policy: EgressPolicy) -> str | None:
    """The Knowledge query for an analysis: the guide of the deterministic detector's top
    scoring family (stages A-B over the stored evidence), or None to keep the Run's own
    question. The guide is methodology only; it never decides the family."""
    detection = detector.detect(
        signals.extract(materials), selection.family_matrix(materials, policy)
    )
    top = max(detection.hypotheses, key=lambda item: item.score, default=None)
    if top is None or top.score < detector.THRESHOLD:
        return None
    return RUNBOOK_QUERIES.get(top.family)


@dataclass(frozen=True)
class Preparation:
    context: PreparedContext | InsufficientContext
    table: signals.SignalTable
    detection: detector.Detection


def prepare(
    materials: list[SlotMaterial],
    scope: ContextScope,
    policy: EgressPolicy,
    profile: ModelProfile,
    analysis_identity: str,
    *,
    knowledge: KnowledgeSelectionView | None = None,
    prompt_version: str = PROMPT_VERSION,
    show_signals: bool = True,
    show_hypotheses: bool = True,
    signal_relevance: bool = True,
    soft_budget: int = selection.SOFT_BUDGET_BYTES,
) -> Preparation:
    """Build the engine context. The keyword knobs exist for offline ablations only;
    the product uses the defaults."""
    table = signals.extract(materials)
    detection = detector.detect(table, selection.family_matrix(materials, policy))

    def extra(aliases):
        return prompt.sections(
            table if show_signals else None,
            detection if show_hypotheses else None,
            aliases,
        )

    context = selection.assemble(
        materials,
        scope,
        policy,
        profile,
        analysis_identity,
        system_prompt=prompt.PROMPTS[prompt_version],
        prompt_version=prompt_version,
        table=table if signal_relevance else None,
        extra_sections=extra,
        soft_budget=soft_budget,
        knowledge=knowledge,
    )
    return Preparation(context=context, table=table, detection=detection)


def refine(advice: RCAAdvice, context: PreparedContext) -> RCAAdvice:
    """Stage E on a structurally valid answer of an engine context. The kept policy uses
    only the answer and the context; it never adds an alias and keeps the answer when the
    result would not validate."""
    if context.context_version != selection.CONTEXT_VERSION:
        return advice
    checked, _ = consistency.check(advice, None, context, policy=CONSISTENCY_POLICY)
    return checked
