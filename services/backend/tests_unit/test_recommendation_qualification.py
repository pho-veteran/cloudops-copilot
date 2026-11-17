"""Controlled histories prove policy branches, never real six-hour availability."""

import copy
import asyncio
import base64
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from recommendation_fixtures import six_hour_case

from cloudops.features.recommendations.coverage import assess, cpu_points
from cloudops.features.recommendations.qualification import qualify
from cloudops.features.recommendations.sources import (
    Series,
    SourceFailure,
    controller_reasons,
    parse_matrix,
    SourceReader,
)
from cloudops.features.investigations.prometheus_config import QUERY_IDS
from cloudops.features.workloads.scope_config import cluster_fingerprint


@pytest.fixture
def case():
    return six_hour_case()


def states(result):
    return {r.resource: r.state for r in result.resources}


def test_positive_zero_is_present_and_receipt_has_no_provider_fields(case):
    result = qualify(*case)
    assert states(result) == {"cpu": "eligible", "memory": "eligible"}
    assert result.pods[0].memory.ratio == 1
    assert "private-" not in result.model_dump_json()
    assert "recommend" not in result.model_dump(exclude={"limitations"})


def test_ratio_and_actual_distinct_bins():
    points = list(range(0, 21600, 60))
    missing = set(points[::20])
    good = [t for t in points if t not in missing]
    assert assess(0, 21600, good).ratio == 0.95
    assert "coverage_below_threshold" not in assess(0, 21600, good * 3).reasons
    assert "coverage_below_threshold" in assess(0, 21600, good[1:]).reasons
    assert assess(0, 21600, [0] * 50000).covered_bins == 1


def test_gap_and_freshness_thresholds_include_both_edges():
    points = list(range(0, 21601, 5))
    assert "coverage_gap" not in assess(0, 21600, [t for t in points if t >= 300]).reasons
    assert "coverage_gap" in assess(0, 21600, [t for t in points if t >= 305]).reasons
    assert "samples_stale" not in assess(0, 21600, [t for t in points if t <= 21480]).reasons
    assert "samples_stale" in assess(0, 21600, [t for t in points if t <= 21475]).reasons
    assert assess(0, 21600, []).longest_gap_seconds == 21600


def test_cpu_requires_real_raw_support_and_detects_reset():
    points, supported, reset = cpu_points([(t, t + 75) for t in range(-75, 601, 5)], 0, 600)
    assert supported and not reset and points[0] == 0
    assert not cpu_points([(t, t) for t in range(0, 601, 5)], 0, 600)[1]
    assert cpu_points([(-75, 2), (-5, 3), (0, 0), (60, 1)], 0, 60)[2]


def test_entire_missing_replica_cannot_be_hidden_by_perfect_replica(case):
    identity, window, history, source = case
    for observation in history.observations:
        observation.pods.append(
            observation.pods[0].model_copy(update={"uid": "missing-uid", "name": "missing-pod"})
        )
    result = qualify(identity, window, history, source)
    assert states(result) == {"cpu": "insufficient_data", "memory": "insufficient_data"}
    assert len(result.pods) == 2 and result.pods[1].memory.covered_bins == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ("uid", "target_changed"),
        ("template", "template_changed"),
        ("epoch", "ownership_history_invalid"),
        ("owner", "pod_ownership_unproven"),
        ("duplicate", "pod_ownership_unproven"),
        ("wrong_rs_uid", "pod_ownership_ambiguous"),
    ],
)
def test_identity_failures_suppress_both(case, change, reason):
    identity, window, history, source = case
    if change == "uid":
        source.after["metadata"]["uid"] = "replacement"
    elif change == "template":
        source.after["spec"]["template"]["spec"]["containers"][0]["image"] = "changed"
    elif change == "epoch":
        history = replace(history, epoch=1)
    elif change == "owner":
        source.series["kube_pod_owner"][0].labels["owner_name"] = "other"
    elif change == "duplicate":
        source.series["kube_pod_info"] *= 2
    elif change == "wrong_rs_uid":
        history.observations[0].pods[0].replicaset_uid = "wrong-rs-uid"
    result = qualify(identity, window, history, source)
    assert all(r.state == "insufficient_data" for r in result.resources)
    assert (
        any(reason in [*result.reasons, *p.reasons] for p in result.pods)
        or reason in result.reasons
    )


def test_same_name_different_uid_is_unqualified(case):
    identity, window, history, source = case
    history.observations[0].pods = [
        history.observations[0].pods[0].model_copy(update={"uid": "old-uid"})
    ]
    result = qualify(identity, window, history, source)
    assert "pod_name_reused" in result.reasons
    assert all(r.state != "eligible" for r in result.resources)


def test_unrelated_namespace_pods_are_not_selector_overlap(case):
    case[2].observations[0].unrelated_pods = 10
    assert all(r.state == "eligible" for r in qualify(*case).resources)


def test_live_overlapping_deployment_selector_requires_manual_review(case):
    source = case[3]
    other = copy.deepcopy(source.before)
    other["metadata"]["uid"] = "other-deployment"
    assert controller_reasons(source.before, [], [], source.policy, [source.before, other]) == {
        "cpu": ["selector_overlap"],
        "memory": ["selector_overlap"],
    }


@pytest.mark.parametrize(
    "shape", ["ephemeralContainers", "initContainers", "resources", "changed_container"]
)
def test_unsupported_current_pod_shapes_stay_explicit(case, shape):
    source = case[3]
    pod = {
        "metadata": {"labels": {"app": "checkout"}},
        "spec": copy.deepcopy(source.before["spec"]["template"]["spec"]),
    }
    if shape == "changed_container":
        pod["spec"]["containers"][0]["image"] = "other-image"
    else:
        pod["spec"][shape] = [{"name": "unsupported"}]
    reasons = controller_reasons(source.before, [], [], source.policy, pods=[pod])
    assert reasons == {
        "cpu": ["unsupported_pod_resource_shape"],
        "memory": ["unsupported_pod_resource_shape"],
    }


@pytest.mark.parametrize(
    "mode,reason,state",
    [
        ("oom", "recent_oom", "excluded"),
        ("restart", "unexplained_restart", "insufficient_data"),
        ("reset", "counter_reset_history", "insufficient_data"),
        ("gap", "oom_history_incomplete", "insufficient_data"),
        ("failed", "oom_history_incomplete", "insufficient_data"),
    ],
)
def test_memory_safety_preserves_independent_cpu(case, mode, reason, state):
    identity, window, history, source = case
    series = source.series["kube_pod_container_status_restarts_total"][0]
    if mode == "oom":
        source.series["kube_pod_container_status_last_terminated_timestamp"] = [
            Series(
                {"uid": "pod-uid", "container": "app"},
                [(window.end.timestamp(), window.end.timestamp() - 30)],
            )
        ]
        source.series["kube_pod_container_status_last_terminated_reason"] = [
            Series(
                {"uid": "pod-uid", "container": "app", "reason": "OOMKilled"},
                [(window.end.timestamp(), 1)],
            )
        ]
    elif mode == "restart":
        source.series["kube_pod_container_status_restarts_total"] = [
            replace(
                series,
                samples=[(t, int(t > window.start.timestamp() + 60)) for t, _ in series.samples],
            )
        ]
    elif mode == "reset":
        source.series["kube_pod_container_status_restarts_total"] = [
            replace(
                series,
                samples=[(t, int(t < window.start.timestamp() + 60)) for t, _ in series.samples],
            )
        ]
    elif mode == "gap":
        source.series["kube_pod_container_status_restarts_total"] = []
    else:
        history = replace(
            history,
            attempts=[{"started_at": window.start + timedelta(seconds=30), "outcome": "failed"}],
        )
    result = qualify(identity, window, history, source)
    assert states(result) == {"cpu": "eligible", "memory": state}
    assert reason in result.pods[0].memory_reasons


def test_missing_lifetime_does_not_shrink_to_usage_presence(case):
    identity, window, history, source = case
    source.series["kube_pod_created"] = []
    result = qualify(identity, window, history, source)
    assert result.pods[0].start == window.start and result.pods[0].memory.expected_bins == 360
    assert all(r.state == "insufficient_data" for r in result.resources)


def test_missing_attempt_history_suppresses_memory(case):
    identity, window, history, source = case
    result = qualify(identity, window, replace(history, attempts=[]), source)
    assert states(result) == {"cpu": "eligible", "memory": "insufficient_data"}


def test_later_container_start_cannot_erase_an_older_pod_interval(case):
    identity, window, history, source = case
    changed = window.start.timestamp() + 10800
    started = source.series["kube_pod_container_state_started"][0]
    source.series["kube_pod_container_state_started"] = [
        replace(started, samples=[(t, changed) for t, _ in started.samples if t >= changed])
    ]
    cpu = source.series["container_cpu_usage_seconds_total"][0]
    source.series["container_cpu_usage_seconds_total"] = [
        replace(cpu, samples=[(t, v) for t, v in cpu.samples if t >= changed])
    ]
    result = qualify(identity, window, history, source)
    assert result.pods[0].cpu.expected_bins == 360
    assert states(result)["cpu"] == "insufficient_data"


def test_constant_prior_restart_is_not_an_in_window_increment(case):
    for observation in case[2].observations:
        observation.pods[0].containers[0].restart_count = 2
    row = case[3].series["kube_pod_container_status_restarts_total"][0]
    case[3].series["kube_pod_container_status_restarts_total"] = [
        replace(row, samples=[(t, 2) for t, _ in row.samples])
    ]
    assert states(qualify(*case))["memory"] == "eligible"


def test_proven_short_completed_lifetime_has_its_own_denominator(case):
    identity, window, history, source = case
    a, b = window.start.timestamp() + 3600, window.start.timestamp() + 7200
    created = source.series["kube_pod_created"][0]
    source.series["kube_pod_created"] = [
        replace(created, samples=[(t, a) for t, _ in created.samples])
    ]
    source.series["kube_pod_container_state_started"] = [
        replace(created, samples=[(t, a + 1) for t, _ in created.samples if t >= a + 1])
    ]
    source.series["kube_pod_completion_time"] = [
        replace(created, samples=[(t, b) for t, _ in created.samples if t >= b])
    ]
    for observation in history.observations:
        if not a <= observation.observed_at.timestamp() <= b:
            observation.pods = []
    result = qualify(identity, window, history, source)
    assert result.pods[0].lifetime == "completed" and result.pods[0].memory.expected_bins == 60
    assert states(result)["memory"] == "eligible"


@pytest.mark.parametrize("resource", ["cpu", "memory"])
@pytest.mark.parametrize("kind", ["Resource", "ContainerResource"])
@pytest.mark.parametrize("target", ["Utilization", "AverageValue"])
def test_hpa_is_resource_specific(case, resource, kind, target):
    source = case[3]
    hpa = {
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "checkout"},
            "metrics": [
                {
                    "type": kind,
                    "resource" if kind == "Resource" else "containerResource": {
                        "name": resource,
                        "target": {"type": target},
                    },
                }
            ],
        }
    }
    result = controller_reasons(source.before, [hpa], [], source.policy)
    assert (
        result[resource] == ["hpa_resource_owned"]
        and result["memory" if resource == "cpu" else "cpu"] == []
    )


@pytest.mark.parametrize("condition", ["vpa", "custom", "unknown_manager", "untrusted_policy"])
def test_resource_automation_requires_manual_review(case, condition):
    source = case[3]
    hpas = []
    vpas = []
    policy = source.policy
    if condition == "vpa":
        vpas = [{"spec": {"targetRef": {"name": "checkout"}}}]
    elif condition == "custom":
        hpas = [
            {
                "spec": {
                    "scaleTargetRef": {
                        "kind": "Deployment",
                        "apiVersion": "apps/v1",
                        "name": "checkout",
                    },
                    "metrics": [{"type": "External"}],
                }
            }
        ]
    elif condition == "unknown_manager":
        source.before["metadata"]["managedFields"] = [
            {"manager": "automation", "fieldsV1": {"f:spec": {"f:resources": {}}}}
        ]
    else:
        policy = policy.model_copy(update={"operator_managed_uids": []})
    assert all(
        "resource_automation_manual" in r
        for r in controller_reasons(source.before, hpas, vpas, policy).values()
    )


@pytest.mark.parametrize("value", ["NaN", "Inf", "-1", True])
def test_nonfinite_negative_and_boolean_metrics_are_rejected(value):
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"namespace": "demo", "uid": "pod"}, "values": [[5, value]]}],
        },
    }
    with pytest.raises(SourceFailure, match="metrics_invalid"):
        parse_matrix(payload, "demo", 0, 10, [0])


def test_foreign_scope_and_sample_budget_are_failures():
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"namespace": "other"}, "values": []}],
        },
    }
    with pytest.raises(SourceFailure, match="metric_scope_mismatch"):
        parse_matrix(payload, "demo", 0, 10, [0])
    payload["data"]["result"][0] = {"metric": {"namespace": "demo"}, "values": [[5, "0"]]}
    with pytest.raises(SourceFailure, match="metrics_payload_limit"):
        parse_matrix(payload, "demo", 0, 10, [200000])


@pytest.fixture
def configured_source(case, tmp_path, monkeypatch):
    cert = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
    ca = base64.b64encode(cert.read_bytes()).decode()
    kube = {"server": "https://cluster.test", "ca_data": ca, "token": "private-kube-sentinel"}
    identity = case[0].model_copy(update={"cluster_fingerprint": cluster_fingerprint(kube)})
    prom = {
        "schema": "PrometheusReadConfig/v1",
        "endpoint_id": "lab-prometheus",
        "producer_version": "prometheus/test",
        "server": "https://metrics.test",
        "ca_data": ca,
        "token": "private-prom-sentinel",
        "queries": {
            key: {
                "query": 'metric{namespace="{{namespace}}",deployment_uid="{{workload_uid}}"}',
                "unit": "count",
            }
            for key in QUERY_IDS
        },
    }
    policy = (
        case[3]
        .policy.model_copy(update={"cluster_fingerprint": identity.cluster_fingerprint})
        .model_dump()
    )
    for env, value in (
        ("KUBE_READ_CONFIG", kube),
        ("PROMETHEUS_READ_CONFIG", prom),
        ("RECOMMENDATION_SOURCE_POLICY", policy),
    ):
        path = tmp_path / env
        path.write_text(json.dumps(value))
        monkeypatch.setenv(env, str(path))
    return identity, case[1], case[3].before


@pytest.mark.parametrize(
    "mode",
    [
        "absent_vpa",
        "hpa_forbidden",
        "discovery_forbidden",
        "vpa_404",
        "timeout",
        "oversized",
        "malformed",
    ],
)
def test_real_reader_contract_distinguishes_absence_from_failed_discovery(configured_source, mode):
    identity, window, deployment = configured_source
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host in {"cluster.test", "metrics.test"}
        path = request.url.path
        status = 200
        if path.endswith("/pods"):
            body = {"kind": "PodList", "items": []}
        elif path.endswith("/deployments"):
            body = {"kind": "DeploymentList", "items": [deployment]}
        elif path == "/apis":
            body = {
                "kind": "APIGroupList",
                "groups": []
                if mode != "vpa_404"
                else [
                    {
                        "name": "autoscaling.k8s.io",
                        "versions": [{"groupVersion": "autoscaling.k8s.io/v1"}],
                    }
                ],
            }
            if mode == "discovery_forbidden":
                status = 403
        elif path.endswith("horizontalpodautoscalers"):
            body = {"kind": "HorizontalPodAutoscalerList", "items": []}
            if mode == "hpa_forbidden":
                status = 403
        elif path.endswith("verticalpodautoscalers"):
            body = {}
            status = 404
        elif path.endswith("/checkout"):
            body = deployment
        else:
            assert path == "/api/v1/query"
            assert (
                "[21676s]" in request.url.params["query"]
                and 'namespace="demo"' in request.url.params["query"]
            )
            if mode == "timeout":
                raise httpx.ReadTimeout("private-provider-sentinel")
            if mode == "oversized":
                return httpx.Response(200, stream=httpx.ByteStream(b"x" * (8 * 1024 * 1024 + 1)))
            if mode == "malformed":
                return httpx.Response(200, stream=httpx.ByteStream(b"private-malformed-sentinel"))
            body = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()))

    reader = SourceReader(transport=httpx.MockTransport(handler))
    if mode == "absent_vpa":
        result = asyncio.run(reader.read(identity, window))
        assert result.controllers == {"cpu": [], "memory": []} and len(calls) == 21
    else:
        with pytest.raises(SourceFailure) as error:
            asyncio.run(reader.read(identity, window))
        assert (
            error.value.reason
            == {
                "hpa_forbidden": "source_permission_denied",
                "discovery_forbidden": "source_permission_denied",
                "vpa_404": "source_unavailable",
                "timeout": "source_timeout",
                "oversized": "metrics_payload_limit",
                "malformed": "source_unavailable",
            }[mode]
        )
        assert "private" not in str(error.value)
