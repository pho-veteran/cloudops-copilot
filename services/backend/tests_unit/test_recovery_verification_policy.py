from copy import deepcopy
from datetime import timedelta

import pytest
from pydantic import ValidationError
from recovery_verification_fixtures import ACK, case, metrics, profile

from cloudops.features.recovery.verification_evaluator import evaluate, metric_summary
from cloudops.features.recovery.verification_policy import VerificationProfile


def verdict(data):
    policy, desired, baseline, samples, raw = data
    return evaluate(policy, ACK, desired, baseline, samples, raw)


@pytest.mark.parametrize(
    "template", ["scale_deployment", "restore_deployment_template", "set_memory_profile"]
)
def test_complete_qualified_conditions_pass_without_causal_or_leak_repair_claim(template):
    result = verdict(case(template))
    assert result["state"] == "passed", result
    assert result["rollout_coverage"] == result["application"]["coverage"] == 1
    assert result["application"]["expected_samples"] == (5 if template == "scale_deployment" else 9)
    if template == "set_memory_profile":
        assert (
            "mitigation" in result["interpretation"]
            and "no repaired-leak" in result["interpretation"]
        )


@pytest.mark.parametrize("key", ["error_ratio", "p95_latency"])
def test_measured_failed_condition_is_failed(key):
    data = case()
    data[-1][key]["series"][0]["samples"][-1][1] = 0.3 if key == "error_ratio" else 0.9
    result = verdict(data)
    assert result["state"] == "failed" and "application_threshold_failed" in result["failed_checks"]


@pytest.mark.parametrize(
    "control",
    [
        "403",
        "missing",
        "gap",
        "duplicate",
        "stale",
        "low",
        "falling",
        "reset",
        "decreasing",
        "wrong_pod",
    ],
)
def test_insufficient_or_incompatible_sources_never_pass(control):
    data = case()
    raw = data[-1]
    if control == "403":
        raw["p95_latency"] = {"error": "forbidden"}
    elif control == "missing":
        raw.pop("requests")
    elif control == "gap":
        raw["error_ratio"]["series"][0]["samples"].pop()
    elif control == "duplicate":
        rows = raw["requests"]["series"][0]["samples"]
        rows.append(rows[-1])
    elif control == "stale":
        raw["sample_time"]["series"][0]["samples"][-1][1] -= 30
    elif control == "low":
        raw["request_rate"]["series"][0]["samples"][-1][1] = 0
    elif control == "falling":
        raw["offered_rate"]["series"][0]["samples"][-1][1] = 6
    elif control == "reset":
        raw["resets"]["series"][0]["samples"][-1][1] = 1
    elif control == "decreasing":
        raw["requests"]["series"][0]["samples"][-1][1] = 0
    else:
        raw["requests"]["series"][0]["pod_uid"] = "replacement"
    assert verdict(data)["state"] == "inconclusive"


@pytest.mark.parametrize(
    "control", ["missing", "late", "wrong_uid", "desired", "replacement", "missing_event_counts"]
)
def test_rollout_and_failure_evidence_have_independent_identity_window_denominator(control):
    data = case()
    samples = data[-2]
    last = samples["6"]
    if control == "missing":
        samples.pop("6")
    elif control == "late":
        last["rollout"]["observed_at"] = (ACK + timedelta(seconds=191)).isoformat()
    elif control == "wrong_uid":
        last["rollout"]["identity"]["uid"] = "wrong"
    elif control == "desired":
        last["rollout"]["deployment"]["desired"] = 2
    elif control == "replacement":
        last["rollout"]["pods"][0]["uid"] = "replacement"
    else:
        last["events"]["redacted_bounded_payload"]["events"] = [{"uid": "event", "type": "Warning"}]
    assert verdict(data)["state"] == "inconclusive"


@pytest.mark.parametrize("kind", ["restart", "oom", "warning", "rollout"])
def test_fresh_measured_failure_is_not_hidden_by_healthy_counters(kind):
    data = case()
    last = data[-2]["6"]
    if kind == "restart":
        last["rollout"]["pods"][0]["containers"][0]["restart_count"] = 1
    elif kind == "oom":
        last["state"]["redacted_bounded_payload"]["pods"][0]["containers"][0]["last_state"] = {
            "terminated": {
                "reason": "OOMKilled",
                "finished_at": (ACK + timedelta(seconds=170)).isoformat(),
            }
        }
    elif kind == "warning":
        last["events"]["redacted_bounded_payload"]["events"] = [
            {
                "uid": "event",
                "type": "Warning",
                "count": 1,
                "last_seen": (ACK + timedelta(seconds=170)).isoformat(),
            }
        ]
    else:
        last["rollout"]["health"] = "progressing"
    assert verdict(data)["state"] == "failed"


def test_settling_warning_is_retained_without_counting_as_a_scored_failure():
    data = case()
    event = {
        "uid": "startup",
        "type": "Warning",
        "count": 1,
        "last_seen": (ACK + timedelta(seconds=5)).isoformat(),
    }
    for sample in data[-2].values():
        sample["events"]["redacted_bounded_payload"]["events"] = [event]
    result = verdict(data)
    assert result["state"] == "passed"
    assert result["event_deltas"] == [{"slot": 0, "uid": "startup", "delta": 1}]


@pytest.mark.parametrize(
    "finished_offset,expected",
    [(10, "failed"), (-1, "passed"), (181, "inconclusive"), (None, "inconclusive")],
)
@pytest.mark.parametrize("missing_events", [False, True])
def test_current_oom_of_excluded_owned_pod_cannot_be_hidden_by_healthy_replacement(
    finished_offset, expected, missing_events
):
    data = case()
    sample = data[-2]["1"]
    pod = deepcopy(sample["rollout"]["pods"][0])
    pod.update(
        uid="failed-owned-pod", name="failed-owned-pod", phase="Failed", excluded=True, ready=False
    )
    pod["containers"][0].update(state="terminated", exit_code=137, ready=False)
    sample["rollout"]["pods"].append(pod)
    sample["state"]["redacted_bounded_payload"]["pods"].append(
        {
            "uid": pod["uid"],
            "containers": [
                {
                    "name": "app",
                    "kind": "regular",
                    "state": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "finished_at": (ACK + timedelta(seconds=finished_offset)).isoformat()
                            if finished_offset is not None
                            else None,
                        }
                    },
                }
            ],
        }
    )
    if missing_events:
        sample["events"]["coverage"]["status"] = "unavailable"
    result = verdict(data)
    assert result["state"] == expected
    assert ("new_oom" in result["failed_checks"]) is (expected == "failed")


def test_same_pod_restart_counter_reset_during_settling_remains_inconclusive():
    data = case()
    data[2]["rollout"]["pods"][0]["containers"][0]["restart_count"] = 1
    result = verdict(data)
    assert result["state"] == "inconclusive"
    assert "restart_counter_reset" in result["reason_codes"]


def test_missing_compatible_counter_during_settling_is_not_forgotten():
    data = case()
    data[2]["rollout"]["pods"][0]["containers"][0]["restart_count"] = 1
    data[-2]["0"]["rollout"]["pods"][0]["containers"][0]["restart_count"] = None
    data[-2]["0"]["rollout"]["coverage"] = "partial"
    result = verdict(data)
    assert (
        result["state"] == "inconclusive"
        and "restart_counter_unavailable" in result["reason_codes"]
    )


def test_original_input_is_not_mutated_and_duplicate_sample_key_cannot_inflate_denominator():
    data = case()
    before = deepcopy(data)
    first, second = verdict(data), verdict(data)
    assert first == second and data == before
    assert first["valid_rollout_samples"] == first["expected_rollout_samples"] == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_coverage", 0.89),
        ("window_seconds", 300),
        ("minimum_rps", 0),
        ("maximum_p95_seconds", float("nan")),
        ("replicas", [True]),
        ("replicas", [4]),
    ],
)
def test_unqualified_policy_shapes_fail_closed(field, value):
    raw = profile().model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        VerificationProfile.model_validate(raw)


def test_lower_completed_request_denominator_cannot_hide_behind_a_rate_gauge():
    p = profile()
    start, end = ACK, ACK + timedelta(seconds=60)
    raw = metrics(start, end)
    for sample in raw["requests"]["series"][0]["samples"]:
        sample[1] = 1000
    assert metric_summary(p, raw, start, end)["state"] == "inconclusive"
