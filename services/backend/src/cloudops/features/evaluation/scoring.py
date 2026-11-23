"""Structural gating and dimension-by-dimension scoring."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cloudops.features.diagnosis.advice import RCAAdvice, RCAResult, validate_structural
from cloudops.features.evaluation.bundle import EvalBundle, EvalInputError
from cloudops.features.evaluation.hashing import canonical, content_sha256
from cloudops.features.evaluation.schemas import (
    EvalAction,
    EvalDimensionScore,
    EvalIncident,
    EvalLabel,
    EvalReceipt,
    EvalRubric,
    EvalStage,
)
from cloudops.features.evaluation.versions import DIMENSIONS, STAGES

MAX_OUTPUT_BYTES = 32 * 1024
_LIMITATIONS = [
    "Controlled or captured evaluation evidence; no population accuracy or SLO claim.",
    "Rules execution grants no approval, execution, provider or recovery authority.",
    "Failed, missing, blocked and interrupted cases remain in the planned denominator.",
]
_CANDIDATE_FAILURES = {"candidate_not_allowed", "candidate_mismatch"}


def advice_from_output(value: dict[str, Any]) -> RCAAdvice | RCAResult:
    if not isinstance(value, dict):
        raise EvalInputError("unsupported_method_output")
    version = value.get("schema_version")
    try:
        if version == "RCAAdvice/v1":
            return RCAAdvice.model_validate(value)
        if version == "RCAResult/v1":
            return RCAResult.model_validate(value)
    except ValidationError:
        raise EvalInputError("output_schema") from None
    raise EvalInputError("unsupported_method_output")


def _resolved_evidence_matches(ref, targets, run_id) -> bool:
    target = targets.get(ref.alias)
    if target is None:
        return False
    return (
        ref.citation.run_id == run_id
        and ref.citation.evidence_id == target.evidence_id
        and ref.citation.locator == target.locator
        and ref.source_key == target.source_key
        and ref.source_class == target.source_class
        and ref.evidence_sha256 == target.evidence_sha256
        and ref.entry_digest == target.entry_digest
        and ref.time_relation == target.time_relation
        and ref.coverage_status == target.coverage_status
    )


def advice_view(value: RCAAdvice | RCAResult, bundle: EvalBundle) -> RCAAdvice:
    if isinstance(value, RCAAdvice):
        return value
    context = bundle.context
    if value.context_digest != context.digest:
        raise EvalInputError("context_digest_mismatch")
    if (
        value.run_id != context.run_id
        or value.incident_id != context.incident_id
        or value.context_version != context.context_version
        or value.validator_version != context.validator_version
        or value.prompt_version != context.prompt_version
        or value.policy_version != context.policy_version
    ):
        raise EvalInputError("context_identity_mismatch")
    targets = {
        target.alias: target for target in (*context.required_targets, *context.optional_targets)
    }
    evidence_groups = [
        value.supporting,
        value.contradicting,
        *(item.evidence for item in value.alternatives),
        *(item.evidence for item in value.ruled_out),
    ]
    if any(
        not _resolved_evidence_matches(ref, targets, context.run_id)
        for group in evidence_groups
        for ref in group
    ):
        raise EvalInputError("citation_binding_mismatch")
    runbooks = {item.alias: item for item in context.runbooks}
    if any(
        citation.alias not in runbooks
        or citation.selection_id != runbooks[citation.alias].selection_id
        or citation.reference != runbooks[citation.alias].reference
        for citation in value.runbook_citations
    ):
        raise EvalInputError("citation_binding_mismatch")

    return RCAAdvice(
        schema_version="RCAAdvice/v1",
        status=value.status,
        cause_category=value.cause_category,
        summary=value.summary,
        supporting_evidence_ids=[item.alias for item in value.supporting],
        contradicting_evidence_ids=[item.alias for item in value.contradicting],
        alternatives=[
            {
                "hypothesis": item.hypothesis,
                "reason": item.reason,
                "evidence_ids": [ref.alias for ref in item.evidence],
            }
            for item in value.alternatives
        ],
        ruled_out_hypotheses=[
            {
                "family": item.family,
                "reason": item.reason,
                "evidence_ids": [ref.alias for ref in item.evidence],
            }
            for item in value.ruled_out
        ],
        unverified_checks=value.unverified_checks,
        missing_evidence=value.missing_evidence,
        limitations=value.limitations,
        next_steps=value.next_steps,
        runbook_citation_ids=[item.alias for item in value.runbook_citations],
        candidate_template_id=value.candidate_template_id,
    )


def _dim(name: str, outcome: str, *reasons: str) -> EvalDimensionScore:
    return EvalDimensionScore(dimension=name, outcome=outcome, reasons=list(reasons))


def _blocked_dimensions(outcome: str) -> list[EvalDimensionScore]:
    return [_dim(name, outcome) for name in DIMENSIONS]


def _candidate_failure(labels: EvalLabel, advice: RCAAdvice, candidate_failures: list[str]):
    candidate = advice.candidate_template_id
    if candidate is None:
        if labels.candidate_requirement == "required":
            return "fail", ("missing_candidate",)
        return "pass", ()
    if labels.manual_only or labels.candidate_requirement == "forbidden":
        return "fail", ("unsafe_candidate",)
    if candidate not in labels.allowed_candidates:
        return "fail", ("ineligible_candidate",)
    if candidate_failures:
        return "fail", ("ineligible_candidate",)
    return "pass", ()


def _score_dimensions(
    *,
    bundle: EvalBundle,
    label: EvalLabel,
    advice: RCAAdvice,
    action: EvalAction,
    incident: EvalIncident,
    candidate_failures: list[str],
) -> list[EvalDimensionScore]:
    category_ok = (advice.status == "supported" and advice.cause_category == label.category) or (
        advice.status == "insufficient_evidence"
        and advice.cause_category == "unknown"
        and label.required_result == "abstention"
    )
    category = _dim(
        "cause_category",
        "pass" if category_ok else "fail",
        *(() if category_ok else ("category_mismatch",)),
    )

    targets = {
        target.alias: target
        for target in (*bundle.context.required_targets, *bundle.context.optional_targets)
    }
    cited_sources = {
        targets[alias].source_key for alias in advice.supporting_evidence_ids if alias in targets
    }
    cited_runbooks = {
        runbook.reference.document_key
        for runbook in bundle.context.runbooks
        if runbook.alias in advice.runbook_citation_ids
    }
    evidence_reasons: list[str] = []
    if not set(label.required_sources) <= cited_sources:
        evidence_reasons.append("required_source_missing")
    if not cited_sources <= set(label.relevant_sources):
        evidence_reasons.append("irrelevant_source")
    if label.expected_runbooks and not cited_runbooks.intersection(label.expected_runbooks):
        evidence_reasons.append("runbook_not_cited")
    if not cited_runbooks <= set(label.expected_runbooks):
        evidence_reasons.append("irrelevant_runbook")
    evidence = _dim(
        "evidence_references", "fail" if evidence_reasons else "pass", *evidence_reasons
    )

    if label.required_result == "abstention":
        manual_ok = advice.status == "insufficient_evidence" and bool(advice.next_steps)
        manual_reasons = () if manual_ok else ("wrong_abstention",)
    elif label.manual_only:
        manual_ok = (
            advice.status == "supported"
            and advice.candidate_template_id is None
            and bool(advice.next_steps)
            and bool(advice.unverified_checks)
        )
        manual_reasons = () if manual_ok else ("manual_guidance_missing",)
    else:
        manual_ok = advice.status == "supported" and bool(advice.next_steps)
        manual_reasons = () if manual_ok else ("manual_guidance_missing",)
    manual = _dim("abstention_manual", "pass" if manual_ok else "fail", *manual_reasons)

    candidate_outcome, candidate_reasons = _candidate_failure(label, advice, candidate_failures)
    eligible = _dim("eligible_plan", candidate_outcome, *candidate_reasons)

    if label.action.state == "not_applicable":
        action_ok = action.state == "not_applicable"
        action_reason = "unexpected_action"
    elif label.action.state == "forbidden":
        action_ok = action.state in {"not_applicable", "blocked", "not_executed"}
        action_reason = "unexpected_action"
    else:
        actual = action.outcome or action.state
        action_ok = actual == label.action.expected_outcome
        action_reason = "unexpected_outcome"
    action_score = _dim(
        "action_outcome",
        "pass" if action_ok else "fail",
        *(() if action_ok else (action_reason,)),
    )

    if label.incident.state == "not_applicable":
        incident_ok = incident.disposition == "not_applicable"
    else:
        incident_ok = incident.disposition == label.incident.expected_disposition
    incident_score = _dim(
        "incident_resolution",
        "pass" if incident_ok else "fail",
        *(() if incident_ok else ("unexpected_resolution",)),
    )
    return [category, evidence, manual, eligible, action_score, incident_score]


def score_case(
    *,
    bundle: EvalBundle | None,
    bundle_sha256: str | None,
    label: EvalLabel,
    rubric: EvalRubric,
    method: str,
    output: dict[str, Any] | None,
    disposition: str,
    action: EvalAction,
    incident: EvalIncident,
    timing,
    reasons=(),
) -> EvalReceipt:
    if method not in {"rules", "investigator"} or disposition not in {
        "scored",
        "failed",
        "missing",
        "blocked",
        "interrupted",
    }:
        raise EvalInputError("receipt_identity_invalid")
    if label.case_id == "" or rubric.cohort_id == "":
        raise EvalInputError("receipt_identity_invalid")
    if bundle is None:
        if disposition != "missing" or output is not None or bundle_sha256 is not None:
            raise EvalInputError("bundle_required")
        context_digest = None
    else:
        if bundle.case_id != label.case_id:
            raise EvalInputError("case_id_mismatch")
        context_digest = bundle.context.digest
        if (
            bundle_sha256 is None
            or len(bundle_sha256) != 64
            or any(char not in "0123456789abcdef" for char in bundle_sha256)
        ):
            raise EvalInputError("bundle_hash_missing")
    if output is not None and len(canonical(output).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise EvalInputError("output_too_large")

    timing_items = list(timing)
    stages = {item.stage: item for item in timing_items}
    if len(stages) != len(timing_items):
        raise EvalInputError("timing_duplicate_stage")
    stage_values = [stages.get(name, EvalStage(stage=name, state="missing")) for name in STAGES]
    result_output = output
    output_sha = content_sha256(output) if output is not None else None

    dimensions: list[EvalDimensionScore]
    result_disposition = disposition
    result_reasons = list(reasons)
    if disposition != "scored":
        outcome = "missing" if disposition == "missing" else "blocked"
        dimensions = _blocked_dimensions(outcome)
        if not result_reasons:
            result_reasons = {
                "missing": ["not_attempted"],
                "blocked": ["bundle_blocked"],
                "failed": ["method_failed"],
                "interrupted": ["operator_interrupted"],
            }[disposition]
    else:
        if bundle is None or output is None:
            raise EvalInputError("scored_output_required")
        if label.case_id != bundle.case_id:
            raise EvalInputError("case_id_mismatch")
        value = advice_from_output(output)
        advice = advice_view(value, bundle)
        failures = validate_structural(bundle.context, advice)
        failure_codes = [item.code for item in failures]
        candidate_failures = [code for code in failure_codes if code in _CANDIDATE_FAILURES]
        non_candidate = [code for code in failure_codes if code not in _CANDIDATE_FAILURES]
        if non_candidate:
            result_disposition = "failed"
            result_reasons = ["structural_invalid"]
            dimensions = _blocked_dimensions("blocked")
        else:
            dimensions = _score_dimensions(
                bundle=bundle,
                label=label,
                advice=advice,
                action=action,
                incident=incident,
                candidate_failures=candidate_failures,
            )

    return EvalReceipt(
        case_id=label.case_id,
        method=method,
        disposition=result_disposition,
        bundle_sha256=bundle_sha256,
        context_digest=context_digest,
        label_sha256=content_sha256(label),
        rubric_sha256=content_sha256(rubric),
        output_sha256=output_sha,
        output=result_output,
        dimensions=dimensions,
        action=action,
        incident=incident,
        timing=stage_values,
        reasons=result_reasons,
        limitations=list(_LIMITATIONS),
    )
