import pytest

from cloudops.features.evaluation.schemas import EvalStage
from cloudops.features.evaluation.summary import summarize
from cloudops.features.evaluation.versions import DIMENSIONS, STAGES
from eval_fixtures import CASES, cohort, labels, receipt, rubric

BASE_REVISION = "0" * 40
SEAL_HASH = "1" * 64


def _summary(receipts, method="rules"):
    return summarize(
        cohort(),
        labels(),
        rubric(),
        receipts,
        method,
        source_revision=BASE_REVISION,
        seal_sha256=SEAL_HASH,
    )


def test_failed_blocked_interrupted_and_missing_stay_in_denominator():
    value = _summary(
        [
            receipt("crashloop-positive", "rules", "scored"),
            receipt("malformed-evidence", "rules", "blocked"),
            receipt("plan-unsafe", "rules", "failed"),
            receipt("oom-positive", "rules", "interrupted"),
        ]
    )
    assert value.planned == 10
    assert value.attempted == 4
    assert value.dispositions == {
        "scored": 1,
        "failed": 1,
        "missing": 6,
        "blocked": 1,
        "interrupted": 1,
    }


def test_planned_count_follows_each_cases_method_list():
    assert _summary([]).planned == 10
    assert _summary([], "investigator").planned == 11


def test_timing_reports_counts_lower_median_and_range():
    receipts = [
        receipt(
            "crashloop-positive",
            "rules",
            "scored",
            timing=[EvalStage(stage="analyze", state="observed", duration_ms=10)],
        ),
        receipt(
            "oom-positive",
            "rules",
            "scored",
            timing=[EvalStage(stage="analyze", state="observed", duration_ms=30)],
        ),
        receipt(
            "traffic-positive",
            "rules",
            "scored",
            timing=[EvalStage(stage="analyze", state="not_applicable")],
        ),
    ]
    analyze = next(item for item in _summary(receipts).stages if item.stage == "analyze")
    assert (analyze.observed, analyze.missing, analyze.not_applicable) == (2, 7, 1)
    assert (analyze.median_ms, analyze.min_ms, analyze.max_ms) == (10, 10, 30)


def test_thresholds_use_the_planned_positive_denominator():
    value = _summary(
        [
            receipt("crashloop-positive", "rules", "scored", passes=("cause_category",)),
            receipt("plan-unsafe", "rules", "scored", passes=("cause_category",)),
        ]
    )
    assert value.thresholds_met is False
    assert value.thresholds["rules_positive_correct_min"] == pytest.approx(2 / 6)
    assert any("rules_positive_correct_min" in reason for reason in value.threshold_reasons)
    assert value.source_revision == BASE_REVISION
    assert value.seal_sha256 == SEAL_HASH


def test_all_clean_scored_receipts_meet_precommitted_thresholds():
    receipts = [
        receipt(spec.case_id, "rules", "scored", passes=DIMENSIONS)
        for spec in CASES
        if "rules" in spec.methods
    ]
    value = _summary(receipts)
    assert value.thresholds_met is True
    assert value.threshold_reasons == []
    assert len(value.stages) == len(STAGES)


def test_summary_disclaims_slo_and_execution_authority():
    text = " ".join(_summary([]).limitations).lower()
    assert "slo" in text
    assert "authority" in text
