from cloudops.features.evaluation.hashing import canonical
from cloudops.features.evaluation.rules import compare
from eval_fixtures import bundle_for


def test_comparator_is_repeatable_for_identical_context():
    bundle, _ = bundle_for("crashloop-positive")
    assert canonical(compare(bundle.context)) == canonical(compare(bundle.context))


def test_rules_emit_expected_supported_and_manual_categories():
    for case_id, category in (
        ("crashloop-positive", "crashloop_startup"),
        ("oom-positive", "oom_restarts"),
        ("traffic-positive", "traffic_latency_errors"),
        ("pending-manual", "pending_unschedulable"),
        ("imagepull-manual", "image_pull"),
    ):
        bundle, _ = bundle_for(case_id)
        advice = compare(bundle.context)
        assert advice.status == "supported"
        assert advice.cause_category == category
        assert (
            advice.candidate_template_id is None
            if category
            in {
                "pending_unschedulable",
                "image_pull",
            }
            else advice.candidate_template_id is not None
        )


def test_healthy_control_abstains_without_a_candidate():
    bundle, _ = bundle_for("healthy-abstention")
    advice = compare(bundle.context)
    assert advice.status == "insufficient_evidence"
    assert advice.cause_category == "unknown"
    assert advice.candidate_template_id is None


def test_hostile_log_text_does_not_change_the_clean_control_advice():
    clean, _ = bundle_for("crashloop-positive")
    injected, _ = bundle_for("injection-control")
    clean_advice = compare(clean.context)
    injected_advice = compare(injected.context)
    assert "SYSTEM OVERRIDE" in " ".join(
        str(target.value)
        for target in (*injected.context.required_targets, *injected.context.optional_targets)
    )
    assert canonical(injected_advice) == canonical(clean_advice)
