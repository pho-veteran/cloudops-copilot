import pytest

from cloudops.features.evaluation.bundle import EvalInputError
from cloudops.features.evaluation.rules import compare
from cloudops.features.evaluation.schemas import EvalAction, EvalIncident, EvalStage
from cloudops.features.evaluation.scoring import score_case
from eval_fixtures import bundle_for, label_for, result_for, rubric


def _score(case_id, advice, *, label=None):
    bundle, bundle_sha = bundle_for(case_id)
    return score_case(
        bundle=bundle,
        bundle_sha256=bundle_sha,
        label=label or label_for(case_id),
        rubric=rubric(),
        method="rules",
        output=advice.model_dump(mode="json"),
        disposition="scored",
        action=EvalAction(state="not_applicable", outcome=None),
        incident=EvalIncident(disposition="not_applicable"),
        timing=[EvalStage(stage="analyze", state="observed", duration_ms=5)],
    )


def _dimension(receipt, name):
    return next(item for item in receipt.dimensions if item.dimension == name)


def test_candidate_safety_is_scored_separately_from_cause():
    bundle, _ = bundle_for("plan-unsafe")
    receipt = _score("plan-unsafe", compare(bundle.context))
    assert _dimension(receipt, "cause_category").outcome == "pass"
    assert _dimension(receipt, "eligible_plan").reasons == ["unsafe_candidate"]


def test_missing_required_candidate_is_a_plan_miss_not_structural_failure():
    bundle, _ = bundle_for("plan-missing")
    advice = compare(bundle.context).model_copy(update={"candidate_template_id": None})
    receipt = _score("plan-missing", advice)
    assert receipt.disposition == "scored"
    assert _dimension(receipt, "eligible_plan").reasons == ["missing_candidate"]


def test_resolved_result_must_match_the_bundle_context():
    bundle, bundle_sha = bundle_for("crashloop-positive")
    result = result_for(bundle.context, compare(bundle.context)).model_copy(
        update={"context_digest": "0" * 64}
    )
    with pytest.raises(EvalInputError, match="context_digest_mismatch"):
        score_case(
            bundle=bundle,
            bundle_sha256=bundle_sha,
            label=label_for("crashloop-positive"),
            rubric=rubric(),
            method="investigator",
            output=result.model_dump(mode="json"),
            disposition="scored",
            action=EvalAction(state="not_applicable", outcome=None),
            incident=EvalIncident(disposition="not_applicable"),
            timing=[],
        )


def test_resolved_result_must_match_context_identity_and_versions():
    bundle, bundle_sha = bundle_for("crashloop-positive")
    result = result_for(bundle.context, compare(bundle.context)).model_copy(
        update={"policy_version": "other-policy"}
    )
    with pytest.raises(EvalInputError, match="context_identity_mismatch"):
        score_case(
            bundle=bundle,
            bundle_sha256=bundle_sha,
            label=label_for("crashloop-positive"),
            rubric=rubric(),
            method="investigator",
            output=result.model_dump(mode="json"),
            disposition="scored",
            action=EvalAction(state="not_applicable", outcome=None),
            incident=EvalIncident(disposition="not_applicable"),
            timing=[],
        )


def test_action_and_resolution_are_scored_independently():
    bundle, bundle_sha = bundle_for("crashloop-positive")
    label = label_for(
        "crashloop-positive",
        action={"state": "expected", "expected_outcome": "succeeded"},
        incident={"state": "expected", "expected_disposition": "resolved"},
    )
    advice = compare(bundle.context)
    receipt = score_case(
        bundle=bundle,
        bundle_sha256=bundle_sha,
        label=label,
        rubric=rubric(),
        method="rules",
        output=advice.model_dump(mode="json"),
        disposition="scored",
        action=EvalAction(state="executed", outcome="blocked"),
        incident=EvalIncident(disposition="unresolved"),
        timing=[],
    )
    assert _dimension(receipt, "cause_category").outcome == "pass"
    assert _dimension(receipt, "action_outcome").outcome == "fail"
    assert _dimension(receipt, "incident_resolution").outcome == "fail"
