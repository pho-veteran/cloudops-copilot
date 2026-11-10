import asyncio
import base64
import copy
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from test_kubernetes_collector import configured_client, log_fake, log_pod, make_context
from test_kubernetes_incident_packs import FakeCluster, pod

from cloudops.features.investigations import kubernetes_collector as kube
from cloudops.features.investigations.catalog import derive_pack_coverage
from cloudops.features.investigations.schemas import Coverage


def install(monkeypatch, fake):
    monkeypatch.setattr(kube, "_scoped_client", lambda *_: fake)


def test_configmap_request_negotiates_metadata_and_rejects_full_objects(tmp_path):
    client = configured_client(tmp_path)
    requested = []

    def metadata(path, params=None, **kwargs):
        requested.append(kwargs.get("accept"))
        return {
            "kind": "PartialObjectMetadata",
            "apiVersion": "meta.k8s.io/v1",
            "metadata": {"name": "app", "namespace": "demo", "uid": "cm-uid"},
        }

    client._request_json = metadata
    client.get_configmap_metadata("demo", "app")
    assert requested == ["application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"]
    client._request_json = lambda *args, **kwargs: {
        "kind": "ConfigMap",
        "data": {"password": "private-sentinel"},
    }
    with pytest.raises(kube.ScopedK8sError) as error:
        client.get_configmap_metadata("demo", "app")
    assert "private-sentinel" not in str(error.value)


def test_noncontroller_reference_does_not_establish_pod_ownership():
    value = pod("unrelated")
    value["metadata"]["ownerReferences"] = [
        {"uid": "other-rs", "kind": "ReplicaSet", "apiVersion": "apps/v1", "controller": True},
        {"uid": "rs-uid-1", "kind": "ReplicaSet", "apiVersion": "apps/v1", "controller": False},
    ]
    assert kube._owned_pods([value], {"rs-uid-1"}, "demo") == []


def test_ambiguous_controller_chain_fails_closed():
    value = pod("ambiguous")
    value["metadata"]["ownerReferences"].append(
        {"uid": "other-rs", "kind": "ReplicaSet", "apiVersion": "apps/v1", "controller": True}
    )
    with pytest.raises(kube.ScopedK8sError, match="ownership_unproven"):
        kube._owned_pods([value], {"rs-uid-1"}, "demo")


def test_same_name_pod_replacement_does_not_publish_replacement_logs(monkeypatch):
    context = make_context()
    value = log_pod()
    fake = log_fake(
        context,
        value,
        lambda *args, **kwargs: ("2026-01-01T00:00:00Z private replacement log\n", False),
    )
    calls = []

    def current(*args):
        calls.append(True)
        result = copy.deepcopy(value)
        if len(calls) > 1:
            result["metadata"]["uid"] = "replacement-uid"
        return result

    fake.get_pod = current
    install(monkeypatch, fake)
    result = kube.collect_kubernetes_logs_current(context, 5)
    assert result.coverage == Coverage(status="unavailable", reason_codes=["identity_changed"])
    assert result.redacted_bounded_payload is None


def test_state_captures_desired_template_revision_and_init_container(monkeypatch):
    context = make_context()
    value = pod("pod-uid-1")
    value["spec"]["initContainers"] = [{"name": "setup", "image": "init:2"}]
    value["status"]["initContainerStatuses"] = [
        {
            "name": "setup",
            "restartCount": 3,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"exitCode": 1}},
        }
    ]
    fake = FakeCluster(pods=[value])
    deployment = fake.get_deployment
    replicasets = fake.list_owned_replicasets

    def desired(*args, **kwargs):
        result = deployment(*args, **kwargs)
        result["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "7"}
        result["spec"]["template"]["spec"]["containers"][0]["image"] = "desired:2"
        return result

    def revision(*args):
        result = replicasets(*args)
        result[0]["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "6"}
        return result

    fake.get_deployment = desired
    fake.list_owned_replicasets = revision
    install(monkeypatch, fake)
    result = kube.collect_kubernetes_state(context, 5)
    payload = result.redacted_bounded_payload
    assert payload["deployment"]["revision"] == "7"
    assert payload["deployment"]["template"]["containers"][0]["image"] == "desired:2"
    assert payload["revisions"][0]["revision"] == "6"
    assert (
        next(row for row in payload["pods"][0]["containers"] if row["name"] == "setup")["state"][
            "waiting"
        ]["reason"]
        == "CrashLoopBackOff"
    )
    assert (
        kube.collect_kubernetes_logs_previous(context, 5).redacted_bounded_payload["logs"][0][
            "kind"
        ]
        == "init"
    )


def test_pod_and_event_caps_are_truthfully_partial_in_uid_order(monkeypatch):
    values = [pod(f"pod-{index:02}") for index in reversed(range(11))]
    fake = FakeCluster(pods=values)
    install(monkeypatch, fake)
    result = kube.collect_kubernetes_state(make_context(), 5)
    assert result.coverage == Coverage(status="partial", reason_codes=["truncated"])
    assert [row["uid"] for row in result.redacted_bounded_payload["pods"]] == [
        f"pod-{index:02}" for index in range(10)
    ]
    fake.pods = values[:1]
    fake.events = [
        {
            "metadata": {"uid": f"event-{index:03}"},
            "involvedObject": {"uid": fake.pods[0]["metadata"]["uid"]},
            "reason": "BackOff",
        }
        for index in reversed(range(101))
    ]
    events = kube.collect_kubernetes_events(make_context(), 5)
    assert events.coverage == Coverage(status="partial", reason_codes=["truncated"])
    assert len(events.redacted_bounded_payload["events"]) == 100
    assert events.redacted_bounded_payload["events"][0]["uid"] == "event-000"


def test_last_page_continuation_discloses_incomplete_listing(tmp_path):
    client = configured_client(tmp_path)
    client._request_json = lambda *args, **kwargs: {
        "kind": "PodList",
        "items": [],
        "metadata": {"continue": "more"},
    }
    assert client._list_items("/api/v1/namespaces/demo/pods", "PodList", "demo") == []
    assert getattr(client, "truncated", False) is True


def test_missing_applicable_previous_log_is_partial_with_other_successes(monkeypatch):
    context = make_context()
    value = log_pod(restart_count=2, containers=[{"name": "app"}, {"name": "sidecar"}])

    def logs(namespace, pod_name, container, **kwargs):
        if container == "sidecar":
            raise kube.ScopedK8sError("container_not_ready")
        return "2026-01-01T00:00:00Z original line\n", False

    install(monkeypatch, log_fake(context, value, logs))
    result = kube.collect_kubernetes_logs_previous(context, 5)
    assert result.coverage == Coverage(status="partial", reason_codes=["no_samples"])
    assert result.redacted_bounded_payload["logs"][0]["text"].endswith("original line\n")


def test_truncated_required_logs_keep_the_pack_partial():
    available = Coverage(status="available", reason_codes=[])
    outcomes = {
        key: available
        for key in ("kubernetes.state", "kubernetes.events", "kubernetes.logs.previous")
    }
    outcomes["kubernetes.logs.current"] = Coverage(status="partial", reason_codes=["truncated"])
    pack = derive_pack_coverage(outcomes)[0]
    assert pack["status"] == "partial"
    assert pack["missing_required"] == ["kubernetes.logs.current"]


def test_windowless_run_makes_no_live_source_calls(monkeypatch):
    calls = []
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    monkeypatch.setattr(kube, "_scoped_client", lambda *_: calls.append(True) or FakeCluster())
    context = replace(make_context(), immutable_window=None)
    for collect in (
        kube.collect_kubernetes_state,
        kube.collect_kubernetes_events,
        kube.collect_kubernetes_logs_current,
        kube.collect_kubernetes_logs_previous,
        kube.collect_kubernetes_configmap_metadata,
    ):
        result = collect(context, 5)
        assert result.coverage == Coverage(status="unavailable", reason_codes=["not_captured"])
    assert calls == []


def test_in_window_logs_do_not_invent_post_window_output(monkeypatch):
    context = make_context()
    install(
        monkeypatch,
        log_fake(
            context,
            log_pod(),
            lambda *args, **kwargs: ("2026-01-01T00:00:00Z original line\n", False),
        ),
    )
    result = kube.collect_kubernetes_logs_current(context, 5)
    assert result.source_times.time_relation == "within_requested_window"
    assert result.source_times.observed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


class Stream(httpx.AsyncByteStream):
    def __init__(self, body, delay=0):
        self.body, self.delay = body, delay

    async def __aiter__(self):
        for byte in self.body:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield bytes([byte])


def transport_client(tmp_path, handler):
    certificate = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
    path = tmp_path / "reader.json"
    path.write_text(
        json.dumps(
            {
                "server": "https://cluster.test",
                "ca_data": base64.b64encode(certificate.read_bytes()).decode(),
                "token": "private-test-token",
            }
        )
    )
    client = kube.ScopedKubernetesClient(str(path))
    client._transport = httpx.MockTransport(handler)
    return client


def test_streaming_transport_cancels_a_trickling_body(tmp_path):
    client = transport_client(
        tmp_path, lambda request: httpx.Response(200, stream=Stream(b"x" * 100, delay=0.01))
    )
    client._timeout = 0.04
    start = time.monotonic()
    with pytest.raises(kube.ScopedK8sError, match="timeout"):
        client._request("/api/v1/namespaces/demo/pods")
    assert time.monotonic() - start < 0.3


def test_transport_cap_and_metadata_header_are_enforced(tmp_path, monkeypatch):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream(b"x" * 64))

    client = transport_client(tmp_path, response)
    monkeypatch.setattr(kube, "MAX_RESPONSE_BYTES", 32)
    with pytest.raises(kube.ScopedK8sError, match="truncated"):
        client._request(
            "/api/v1/namespaces/demo/configmaps/app",
            accept="application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1",
        )
    assert (
        calls[0].headers["accept"] == "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
    )
    assert calls[0].method == "GET"


def test_wrong_owner_api_group_cannot_establish_membership():
    value = pod("wrong-group")
    value["metadata"]["ownerReferences"][0]["apiVersion"] = "batch/v1"
    assert kube._owned_pods([value], {"rs-uid-1"}, "demo") == []
