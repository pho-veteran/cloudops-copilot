"""Denominator-preserving aggregation for evaluation receipts."""

from __future__ import annotations

from statistics import median_low

from cloudops.features.evaluation.schemas import (
    EvalCohort,
    EvalLabels,
    EvalReceipt,
    EvalRubric,
    EvalSummary,
    EvalFamilySummary,
    EvalStageSummary,
)
from cloudops.features.evaluation.versions import DIMENSIONS, DISPOSITIONS, STAGES

_LIMITATIONS = [
    "Failed, missing, blocked and interrupted cases remain in the planned denominator.",
    "Timing counts include missing markers; missing is not zero.",
    "Controlled results do not establish an SLO, population accuracy or execution authority.",
]


def summarize(
    cohort: EvalCohort,
    labels: EvalLabels,
    rubric: EvalRubric,
    receipts: list[EvalReceipt],
    method: str,
    *,
    source_revision: str,
    seal_sha256: str,
) -> EvalSummary:
    if method not in {"rules", "investigator"}:
        raise ValueError("method_invalid")
    if cohort.cohort_id != labels.cohort_id or cohort.cohort_id != rubric.cohort_id:
        raise ValueError("cohort_mismatch")
    planned_cases = [case for case in cohort.cases if method in case.methods]
    planned_by_id = {case.case_id: case for case in planned_cases}
    receipt_by_id: dict[str, EvalReceipt] = {}
    for receipt in receipts:
        if receipt.method != method or receipt.case_id not in planned_by_id:
            raise ValueError("receipt_not_planned")
        if receipt.case_id in receipt_by_id:
            raise ValueError("duplicate_receipt")
        receipt_by_id[receipt.case_id] = receipt

    dispositions = {name: 0 for name in DISPOSITIONS}
    for case in planned_cases:
        receipt = receipt_by_id.get(case.case_id)
        disposition = receipt.disposition if receipt is not None else "missing"
        dispositions[disposition] += 1

    family_ids = sorted({case.family for case in planned_cases})
    families: list[EvalFamilySummary] = []
    for family in family_ids:
        cases = [case for case in planned_cases if case.family == family]
        per_dimension = {dimension: 0 for dimension in DIMENSIONS}
        counts = {name: 0 for name in DISPOSITIONS}
        for case in cases:
            receipt = receipt_by_id.get(case.case_id)
            disposition = receipt.disposition if receipt is not None else "missing"
            counts[disposition] += 1
            if receipt is not None and receipt.disposition == "scored":
                for score in receipt.dimensions:
                    if score.outcome == "pass":
                        per_dimension[score.dimension] += 1
        families.append(
            EvalFamilySummary(
                family=family,
                planned=len(cases),
                scored=counts["scored"],
                failed=counts["failed"],
                missing=counts["missing"],
                blocked=counts["blocked"],
                interrupted=counts["interrupted"],
                per_dimension_passes=per_dimension,
            )
        )

    stages: list[EvalStageSummary] = []
    for name in STAGES:
        observed = []
        missing = 0
        not_applicable = 0
        for case in planned_cases:
            receipt = receipt_by_id.get(case.case_id)
            stage = (
                next((item for item in receipt.timing if item.stage == name), None)
                if receipt is not None
                else None
            )
            state = stage.state if stage is not None else "missing"
            if state == "observed":
                observed.append(stage.duration_ms)
            elif state == "not_applicable":
                not_applicable += 1
            else:
                missing += 1
        durations = [value for value in observed if value is not None]
        stages.append(
            EvalStageSummary(
                stage=name,
                observed=len(durations),
                missing=missing,
                not_applicable=not_applicable,
                median_ms=median_low(durations) if durations else None,
                min_ms=min(durations) if durations else None,
                max_ms=max(durations) if durations else None,
            )
        )

    labels_by_id = labels.cases
    scored = [receipt for receipt in receipts if receipt.disposition == "scored"]
    unsafe = [
        receipt
        for receipt in scored
        if "unsafe_candidate"
        in next(item.reasons for item in receipt.dimensions if item.dimension == "eligible_plan")
    ]
    manual_unsafe = [receipt for receipt in unsafe if labels_by_id[receipt.case_id].manual_only]
    candidate_misses = [
        receipt
        for receipt in scored
        if "missing_candidate"
        in next(item.reasons for item in receipt.dimensions if item.dimension == "eligible_plan")
    ]
    label_leaks = [
        receipt
        for receipt in receipts
        if {"label_leak", "label_content_in_bundle"} & set(receipt.reasons)
    ]

    def pass_count(cases, dimension):
        count = 0
        for case in cases:
            receipt = receipt_by_id.get(case.case_id)
            if receipt is None or receipt.disposition != "scored":
                continue
            if any(
                item.dimension == dimension and item.outcome == "pass"
                for item in receipt.dimensions
            ):
                count += 1
        return count

    positive_cases = [case for case in planned_cases if case.treatment in {"positive", "injection"}]
    manual_cases = [case for case in planned_cases if case.treatment == "manual"]
    abstention_cases = [case for case in planned_cases if case.treatment == "abstention"]
    positive_rate = (
        pass_count(positive_cases, "cause_category") / len(positive_cases)
        if positive_cases
        else 0.0
    )
    manual_rate = (
        pass_count(manual_cases, "abstention_manual") / len(manual_cases) if manual_cases else 0.0
    )
    abstention_rate = (
        pass_count(abstention_cases, "cause_category") / len(abstention_cases)
        if abstention_cases
        else 0.0
    )

    limits = rubric.thresholds
    observed_thresholds: dict[str, bool | int | float] = {
        "structural_valid_required": not any(
            "structural_invalid" in receipt.reasons for receipt in receipts
        ),
        "manual_only_candidate_max": len(manual_unsafe),
        "unsafe_candidate_max": len(unsafe),
        "required_candidate_miss_max": len(candidate_misses),
        "label_leak_max": len(label_leaks),
        "rules_positive_correct_min": round(positive_rate, 6),
        "rules_manual_correct_min": round(manual_rate, 6),
        "rules_abstention_correct_min": round(abstention_rate, 6),
    }
    threshold_reasons: list[str] = []
    if limits.structural_valid_required and not observed_thresholds["structural_valid_required"]:
        threshold_reasons.append("structural_valid_required:false")
    for name in (
        "manual_only_candidate_max",
        "unsafe_candidate_max",
        "required_candidate_miss_max",
        "label_leak_max",
    ):
        if observed_thresholds[name] > getattr(limits, name):
            threshold_reasons.append(f"{name}:{observed_thresholds[name]}>{getattr(limits, name)}")
    for name in (
        "rules_positive_correct_min",
        "rules_manual_correct_min",
        "rules_abstention_correct_min",
    ):
        if observed_thresholds[name] < getattr(limits, name):
            reason = (
                f"{name}:no_planned_cases"
                if name == "rules_positive_correct_min" and not positive_cases
                else f"{name}:{observed_thresholds[name]}<{getattr(limits, name)}"
            )
            threshold_reasons.append(reason)
    limitations = [*_LIMITATIONS, *rubric.limitations]
    if method == "investigator":
        limitations.append("Rules-prefixed thresholds are diagnostic for the Investigator method.")
    return EvalSummary(
        cohort_id=cohort.cohort_id,
        source_revision=source_revision,
        seal_sha256=seal_sha256,
        method=method,
        planned=len(planned_cases),
        attempted=sum(
            1
            for case in planned_cases
            if case.case_id in receipt_by_id
            and receipt_by_id[case.case_id].disposition != "missing"
        ),
        dispositions=dispositions,
        families=families,
        stages=stages,
        thresholds=observed_thresholds,
        thresholds_met=not threshold_reasons,
        threshold_reasons=threshold_reasons,
        limitations=limitations,
    )
