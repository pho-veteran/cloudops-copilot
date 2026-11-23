import pytest
from pydantic import ValidationError

from cloudops.features.evaluation.schemas import EvalCasePlan, EvalLabel, EvalStage


def test_case_bundle_must_be_canonical_and_case_bound():
    with pytest.raises(ValidationError):
        EvalCasePlan(
            case_id="case-a",
            family="oom_restarts",
            treatment="positive",
            bundle="bundles/../private.json",
            methods=["rules"],
            planned_stages=["analyze"],
        )


def test_stage_missing_has_no_duration_and_observed_requires_duration():
    assert EvalStage(stage="analyze", state="missing").duration_ms is None
    with pytest.raises(ValidationError):
        EvalStage(stage="analyze", state="observed")
    with pytest.raises(ValidationError):
        EvalStage(stage="analyze", state="missing", duration_ms=0)


def test_labels_reject_unrecognized_fields():
    with pytest.raises(ValidationError):
        EvalLabel.model_validate(
            {
                "case_id": "case-a",
                "category": "oom_restarts",
                "required_result": "supported",
                "manual_only": False,
                "allowed_candidates": ["set_memory_profile"],
                "candidate_requirement": "required",
                "required_sources": ["kubernetes.state"],
                "relevant_sources": ["kubernetes.state"],
                "expected_runbooks": [],
                "clean_control": None,
                "action": {"state": "not_applicable", "expected_outcome": None},
                "incident": {"state": "not_applicable", "expected_disposition": None},
                "rationale": "controlled test",
                "secret_oracle": "must not pass through",
            }
        )
