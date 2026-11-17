import copy
import json

import pytest
from recommendation_fixtures import runner_case

from cloudops.features.recommendations.krr_protocol import (
    ContractError,
    bound_snapshot,
    normalize,
    parse_json,
    snapshot,
    validate_request,
)
from cloudops.features.recommendations.producer import advice
from cloudops.features.recommendations.qualification import qualify
from cloudops.features.recommendations.sources import Series


def test_pinned_shape_preserves_observed_before_minimum_and_drops_raw_configuration():
    case, body, raw, observed = runner_case()
    validate_request(body, body["profile"]["image_digest"])
    bound_snapshot(snapshot(case[3].before), body["identity"])
    normalized = normalize(raw, observed, body)
    assert "private-" not in json.dumps(normalized)
    evidence = qualify(*case)
    resources = advice(case[0], case[3], evidence, normalized)
    cpu, memory = resources
    assert cpu.state == memory.state == "available"
    assert cpu.recommended_limit.kind == "unset" and cpu.observed.value == "0.025"
    assert memory.observed.value == "25165824" and memory.recommended_request.value == "104857600"
    assert "75-second" in memory.statistic_definition


@pytest.mark.parametrize(
    "change",
    [
        "namespace",
        "container",
        "extra",
        "duplicate",
        "missing",
        "pods",
        "duplicate_pods",
        "profile",
        "correlation",
        "settings",
        "parameters",
    ],
)
def test_foreign_extra_duplicate_or_unbound_tool_output_is_rejected(change):
    case, body, raw, observed = runner_case()
    scan = raw["scans"][0]
    if change == "namespace":
        scan["object"]["namespace"] = "foreign"
    elif change == "container":
        scan["object"]["container"] = "other"
    elif change in {"extra", "duplicate"}:
        raw["scans"].append(copy.deepcopy(scan))
    elif change == "missing":
        raw["scans"] = []
    elif change == "pods":
        observed["observations"][0]["cpu"]["pods"] = ["foreign"]
    elif change == "duplicate_pods":
        scan["object"]["pods"] *= 2
    elif change == "profile":
        raw["strategy"]["name"] = "other"
    elif change == "correlation":
        observed["request_sha256"] = "a" * 64
    elif change == "settings":
        raw["strategy"]["settings"]["allow_hpa"] = True
    else:
        observed["parameters"]["cpu_min"] = 0
    with pytest.raises(ContractError):
        normalize(raw, observed, body)


@pytest.mark.parametrize("number", [True, -1, float("nan"), float("inf"), "10m", {"value": 1}])
def test_invalid_numbers_do_not_become_quantities(number):
    _, body, raw, observed = runner_case()
    raw["scans"][0]["recommended"]["requests"]["cpu"]["value"] = number
    with pytest.raises(ContractError):
        normalize(raw, observed, body)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(ContractError):
        parse_json(raw)


def test_oom_suppresses_all_numeric_memory_advice_without_clamping():
    case, body, raw, observed = runner_case()
    identity, window, history, source = case
    source.series["kube_pod_container_status_last_terminated_reason"] = [
        Series(
            {"uid": "pod-uid", "container": "app", "reason": "OOMKilled"},
            [(window.end.timestamp(), 1)],
        )
    ]
    source.series["kube_pod_container_status_last_terminated_timestamp"] = [
        Series(
            {"uid": "pod-uid", "container": "app"},
            [(window.end.timestamp(), window.end.timestamp() - 30)],
        )
    ]
    resources = advice(
        identity, source, qualify(identity, window, history, source), normalize(raw, observed, body)
    )
    assert resources[0].state == "available"
    assert (
        resources[1].state == "excluded"
        and resources[1].recommended_request.kind
        == resources[1].recommended_limit.kind
        == "unavailable"
    )
    assert "recent_oom" in resources[1].reasons and resources[1].observed is not None


def test_absence_and_true_zero_are_distinct_and_native_minimums_are_kept():
    case, body, raw, observed = runner_case()
    observed["observations"][0]["cpu"]["observed"] = 0
    raw["scans"][0]["recommended"]["requests"]["cpu"]["value"] = 0.01
    cpu = advice(case[0], case[3], qualify(*case), normalize(raw, observed, body))[0]
    assert cpu.observed.value == "0" and cpu.recommended_request.value == "0.01"
    for kind in ("requests", "limits"):
        raw["scans"][0]["recommended"][kind]["cpu"]["value"] = "?"
    observed["observations"][0]["cpu"]["observed"] = None
    cpu = advice(case[0], case[3], qualify(*case), normalize(raw, observed, body))[0]
    assert (
        cpu.state == "insufficient_data"
        and cpu.observed is None
        and cpu.recommended_request.kind == "unavailable"
    )


def test_quantities_and_native_formula_are_verified_again_after_transport():
    case, body, raw, observed = runner_case()
    normalized = normalize(raw, observed, body)
    normalized[0]["resources"]["cpu"]["recommended"]["requests"] = 0.1
    with pytest.raises(ContractError, match="tool_numeric_mismatch"):
        advice(case[0], case[3], qualify(*case), normalized)
    normalized = normalize(raw, observed, body)
    normalized[0]["resources"]["memory"]["current"]["requests"] = 100
    with pytest.raises(ContractError, match="tool_current_mismatch"):
        advice(case[0], case[3], qualify(*case), normalized)


def completed_envelope():
    from datetime import datetime, timezone
    from cloudops.features.recommendations.krr_protocol import digest

    case, body, raw, observed = runner_case()
    now = datetime.now(timezone.utc)
    current = snapshot(case[3].before)
    result = {
        "protocol_version": 1,
        "request_sha256": digest(body),
        "state": "completed",
        "reason": "tool_completed",
        "tool_version": "1.31.1",
        "image_digest": body["profile"]["image_digest"],
        "invocation_started_at": now.isoformat(),
        "invocation_finished_at": now.isoformat(),
        "tool_exit_code": 0,
        "timed_out": False,
        "output_sha256": "c" * 64,
        "scope_before": current,
        "scope_after": copy.deepcopy(current),
        "containers": normalize(raw, observed, body),
    }
    return body, result, now


def test_actual_window_conservatively_contains_native_mixed_query_alignment():
    from datetime import timedelta
    from cloudops.features.recommendations.producer import envelope

    body, result, sent = completed_envelope()
    invocation, window = envelope(result, body, sent)
    assert invocation.started_at == sent == window.end
    assert window.start == sent.replace(second=0, microsecond=0) - timedelta(hours=6)
    assert window.raw_input_start == window.start - timedelta(seconds=75)
    assert window.raw_input_end == window.end and window.kind == "query_envelope"


@pytest.mark.parametrize(
    "change",
    [
        "nonce",
        "image",
        "version",
        "exit",
        "timeout",
        "target",
        "missing",
        "naive",
        "old",
        "future",
        "duration",
    ],
)
def test_replayed_mismatched_or_impossible_receipt_cannot_publish_advice(change):
    from datetime import timedelta
    from cloudops.features.recommendations.producer import envelope

    body, result, sent = completed_envelope()
    if change == "nonce":
        result["request_sha256"] = "f" * 64
    elif change == "image":
        result["image_digest"] = "sha256:" + "f" * 64
    elif change == "version":
        result["tool_version"] = "dev"
    elif change == "exit":
        result["tool_exit_code"] = 1
    elif change == "timeout":
        result["timed_out"] = True
    elif change == "target":
        result["scope_after"]["uid"] = "foreign-uid"
    elif change == "missing":
        result["scope_after"] = None
    elif change == "naive":
        result["invocation_started_at"] = sent.replace(tzinfo=None).isoformat()
    elif change == "old":
        result["invocation_started_at"] = (sent - timedelta(seconds=6)).isoformat()
    elif change == "future":
        result["invocation_finished_at"] = (sent + timedelta(seconds=6)).isoformat()
    else:
        result["invocation_finished_at"] = (sent + timedelta(seconds=151)).isoformat()
    with pytest.raises(ContractError):
        envelope(result, body, sent)


@pytest.mark.parametrize(
    "response", ["redirect", "encoding", "oversize", "duplicate", "timeout", "success"]
)
def test_runner_transport_is_bounded_and_never_follows_redirects(monkeypatch, tmp_path, response):
    import httpx
    from cloudops.features.recommendations import producer

    key = tmp_path / "key"
    key.write_text("controlled-transport-key")
    monkeypatch.setenv("KRR_RUNNER_KEY", str(key))
    monkeypatch.setenv("KRR_RUNNER_URL", "http://runner.test")
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer controlled-transport-key"
        if response == "redirect":
            return httpx.Response(302, headers={"Location": "http://foreign.test/"})
        if response == "encoding":
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"")
        if response == "oversize":
            return httpx.Response(200, stream=httpx.ByteStream(b" " * 262_145))
        if response == "duplicate":
            return httpx.Response(200, stream=httpx.ByteStream(b'{"state":1,"state":2}'))
        if response == "timeout":
            raise httpx.ReadTimeout("controlled timeout")
        return httpx.Response(200, stream=httpx.ByteStream(b'{"state":"unavailable"}'))

    original = httpx.Client
    monkeypatch.setattr(
        producer.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    admitted = []
    if response == "success":
        assert producer.call_runner({}, before_call=lambda: admitted.append(True)) == {
            "state": "unavailable"
        }
    else:
        with pytest.raises((ContractError, httpx.ReadTimeout)):
            producer.call_runner({}, before_call=lambda: admitted.append(True))
    assert admitted == [True] and len(calls) == 1


def test_numeric_tool_output_cannot_bypass_resource_specific_hpa_exclusion():
    case, body, raw, observed = runner_case()
    case[3].controllers["cpu"].append("hpa_cpu")
    cpu, memory = advice(case[0], case[3], qualify(*case), normalize(raw, observed, body))
    assert cpu.state == "excluded" and cpu.recommended_request.kind == "unavailable"
    assert memory.state == "available" and memory.recommended_request.kind == "quantity"


def test_invalid_source_quantity_is_nonavailable_contract_failure():
    case, body, raw, observed = runner_case()
    normalized = normalize(raw, observed, body)
    evidence = qualify(*case)
    case[3].before["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = (
        "invalid"
    )
    with pytest.raises(ContractError, match="tool_current_mismatch"):
        advice(case[0], case[3], evidence, normalized)
