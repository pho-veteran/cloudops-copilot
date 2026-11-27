"""Stage C compact context: zero-call gates on the E06 fixtures."""

import json
from pathlib import Path

import pytest
from diagnosis_fixtures import profile
from eval_fixtures import _material, _policy, _scope

from cloudops.features.diagnosis.advice import validate_structural
from cloudops.features.diagnosis.context import PreparedContext, assemble as product_assemble
from cloudops.features.diagnosis.egress import check_send
from cloudops.features.diagnosis.engine import prompt, selection, signals
from cloudops.features.diagnosis.versions import SOURCE_CLASSES
from cloudops.features.evaluation.rules import compare

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "services/backend/tests/fixtures/e06"
CASES = {item["id"]: item for item in json.loads((FIXTURES / "cases.json").read_text())}
ORACLE = json.loads((FIXTURES / "oracle.json").read_text())["cases"]


def _build(case_id: str, materials=None):
    materials = materials or [
        _material(slot, payload, case_id)
        for slot, payload in CASES[case_id]["evidence"].items()
        if slot in SOURCE_CLASSES
    ]
    context = selection.assemble(
        materials,
        _scope(case_id, CASES[case_id]["question"]),
        _policy(),
        profile(),
        "engine-test",
        system_prompt=prompt.PROMPTS[prompt.PROMPT_V1],
        prompt_version=prompt.PROMPT_V1,
        table=signals.extract(materials),
    )
    return context, materials


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_compact_context_is_small_complete_and_validator_compatible(case_id):
    context, materials = _build(case_id)
    assert isinstance(context, PreparedContext)
    product = product_assemble(
        materials, _scope(case_id, CASES[case_id]["question"]), None, _policy(), profile(), "p"
    )
    assert context.message_bytes < product.message_bytes / 2
    assert context.families == product.families
    sent = {target.source_key for target in (*context.required_targets, *context.optional_targets)}
    oracle = ORACLE[case_id]
    if oracle["required_result"] == "supported":
        assert {
            slot for slot in oracle["relevant_sources"] if slot in CASES[case_id]["evidence"]
        } <= sent
    # The unchanged validator accepts a structurally valid answer over the engine context.
    advice = compare(context)
    assert validate_structural(context, advice) == []
    send = check_send(context, _policy(), profile(), "initial")
    assert send.ok and send.byte_size == context.message_bytes
    assert context.context_version == selection.CONTEXT_VERSION
    assert "Required JSON Schema" not in context.messages[0].content


def test_untrusted_log_text_stays_inside_a_quoted_value():
    context, _ = _build("n-injection")
    lines = [line for line in context.messages[1].content.splitlines() if "SYSTEM OVERRIDE" in line]
    assert len(lines) == 1
    assert lines[0].startswith("E") and '"SYSTEM OVERRIDE' in lines[0]


def test_budget_drops_low_relevance_entries_and_records_omissions():
    case_id = "c03"
    materials = [
        _material(slot, payload, case_id)
        for slot, payload in CASES[case_id]["evidence"].items()
        if slot in SOURCE_CLASSES
    ]
    full = selection.assemble(
        materials,
        _scope(case_id),
        _policy(),
        profile(),
        "engine-test",
        system_prompt=prompt.PROMPTS[prompt.PROMPT_V1],
        prompt_version=prompt.PROMPT_V1,
        table=signals.extract(materials),
    )
    tight = selection.assemble(
        materials,
        _scope(case_id),
        _policy(),
        profile(),
        "engine-test",
        system_prompt=prompt.PROMPTS[prompt.PROMPT_V1],
        prompt_version=prompt.PROMPT_V1,
        table=signals.extract(materials),
        soft_budget=3600,
    )
    assert isinstance(tight, PreparedContext)
    assert len(tight.optional_targets) < len(full.optional_targets)
    assert any(item.code == "budget_omitted" for item in tight.omissions)
    assert "OMITTED" in tight.messages[1].content
    # Each required slot keeps at least one entry.
    assert {t.source_key for t in tight.required_targets} == {
        t.source_key for t in full.required_targets
    }


def test_hypothesis_sections_cite_only_sent_aliases_and_consistency_downgrades():
    from cloudops.features.diagnosis.context import family_matrix
    from cloudops.features.diagnosis.engine import consistency, detector

    case_id = "c03"
    materials = [
        _material(slot, payload, case_id)
        for slot, payload in CASES[case_id]["evidence"].items()
        if slot in SOURCE_CLASSES
    ]
    table = signals.extract(materials)
    detection = detector.detect(table, family_matrix(materials, _policy()))
    context = selection.assemble(
        materials,
        _scope(case_id),
        _policy(),
        profile(),
        "engine-test",
        system_prompt=prompt.PROMPTS[prompt.PROMPT_V2],
        prompt_version=prompt.PROMPT_V2,
        table=table,
        extra_sections=lambda aliases: prompt.sections(table, detection, aliases),
    )
    assert isinstance(context, PreparedContext)
    text = context.messages[1].content
    assert "DETECTOR HYPOTHESES" in text and "1. oom_restarts" in text
    sent = {t.alias for t in (*context.required_targets, *context.optional_targets)}
    import re

    section = text[text.index("DETECTOR") : text.index("EVIDENCE")]
    assert set(re.findall(r"E\d{3}", section)) <= sent
    advice = compare(context)
    assert advice.cause_category == "oom_restarts"
    kept, note = consistency.check(advice, detection, context, policy="downgrade")
    assert kept == advice and note["action"] == "none"
    wrong = advice.model_copy(
        update={"cause_category": "image_pull", "candidate_template_id": None}
    )
    assert validate_structural(context, wrong) == []
    downgraded, note = consistency.check(wrong, detection, context, policy="downgrade")
    assert note["action"] == "downgrade"
    assert downgraded.status == "insufficient_evidence" and downgraded.cause_category == "unknown"
    assert validate_structural(context, downgraded) == []
    flagged, note = consistency.check(wrong, detection, context, policy="flag")
    assert flagged == wrong and note["action"] == "flag"


def test_required_citations_policy_moves_only_non_required_support():
    from cloudops.features.diagnosis.context import family_matrix
    from cloudops.features.diagnosis.engine import consistency, detector
    from cloudops.features.diagnosis.versions import FAMILY_REQUIRED

    case_id = "c03"
    materials = [
        _material(slot, payload, case_id)
        for slot, payload in CASES[case_id]["evidence"].items()
        if slot in SOURCE_CLASSES
    ]
    table = signals.extract(materials)
    detection = detector.detect(table, family_matrix(materials, _policy()))
    context = selection.assemble(
        materials,
        _scope(case_id),
        _policy(),
        profile(),
        "engine-test",
        system_prompt=prompt.PROMPTS[prompt.PROMPT_V3],
        prompt_version=prompt.PROMPT_V3,
        table=table,
    )
    assert isinstance(context, PreparedContext)
    slot_of = {
        t.alias: t.source_key for t in (*context.required_targets, *context.optional_targets)
    }
    advice = compare(context)
    required = set(FAMILY_REQUIRED[advice.cause_category])
    inside = [alias for alias, slot in slot_of.items() if slot in required][:2]
    outside = [alias for alias, slot in slot_of.items() if slot not in required][:1]
    assert inside and outside
    mixed = advice.model_copy(update={"supporting_evidence_ids": sorted(inside + outside)})
    assert validate_structural(context, mixed) == []
    pruned, note = consistency.check(mixed, detection, context, policy="required_citations")
    assert note["action"] == "prune_citations" and note["dropped"] == outside
    assert pruned.supporting_evidence_ids == sorted(inside)
    assert validate_structural(context, pruned) == []
    only_outside = advice.model_copy(update={"supporting_evidence_ids": outside})
    kept, note = consistency.check(only_outside, detection, context, policy="required_citations")
    assert kept == only_outside and note["action"] == "none"
