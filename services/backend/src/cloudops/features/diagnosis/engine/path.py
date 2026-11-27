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
    family_matrix,
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
    detection = detector.detect(table, family_matrix(materials, policy))

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
