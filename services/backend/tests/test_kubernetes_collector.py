from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cloudops.features.investigations import kubernetes_collector
from cloudops.features.investigations.collector import (
    CollectionContext,
    ScopeSnapshot,
    WindowSnapshot,
)
from cloudops.features.investigations.kubernetes_collector import (
    ScopedK8sError,
    ScopedKubernetesClient,
    collect_kubernetes_events,
    collect_kubernetes_logs_current,
    collect_kubernetes_logs_previous,
    collect_kubernetes_state,
)
from cloudops.features.investigations.schemas import Coverage


def test_rbac_manifest_authorizes_pods_log_and_excludes_secrets():
    from pathlib import Path

    import yaml

    manifest = Path("config/lab/workload.yaml").read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(manifest))
    reader_role = next(
        doc
        for doc in docs
        if doc.get("kind") == "Role" and doc.get("metadata", {}).get("name") == "cloudops-reader"
    )
    # Must have get on pods/log
    rules = reader_role["rules"]
    assert any(
        rule.get("resources") == ["pods/log"] and rule.get("verbs") == ["get"] for rule in rules
    ), "cloudops-reader must have get on pods/log"
    # Must never have secrets in any rule
    for rule in rules:
        assert "secrets" not in rule.get("resources", [])


def test_client_rejects_missing_config(monkeypatch):
    monkeypatch.delenv("KUBE_READ_CONFIG", raising=False)
    with pytest.raises(ScopedK8sError) as exc:
        ScopedKubernetesClient(config_path=None)
    assert exc.value.code == "cluster_unavailable"


def test_client_enforces_deployment_uid_match(tmp_path):
    import json

    cfg = tmp_path / "k8s.json"
    cfg.write_text(
        json.dumps(
            {
                "server": "https://127.0.0.1:6443",
                "ca_data": "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==",
                "token": "secret-token",
            }
        )
    )
    client = ScopedKubernetesClient(config_path=str(cfg))
    # Mock transport returning UID mismatch
    client._request_json = lambda path, params=None: {
        "metadata": {"uid": "wrong-uid", "name": "checkout", "namespace": "demo"}
    }
    with pytest.raises(ScopedK8sError) as exc:
        client.get_deployment("demo", "checkout", expected_uid="expected-uid")
    assert exc.value.code == "target_changed"


def test_client_maps_http_403_to_forbidden_code(monkeypatch):
    client = ScopedKubernetesClient(config_path="ignore")

    def raise_forbidden(path, params=None):
        raise ScopedK8sError("forbidden")

    client._request_json = raise_forbidden
    with pytest.raises(ScopedK8sError) as exc:
        client.get_deployment("demo", "checkout", expected_uid="uid")
    assert exc.value.code == "forbidden"


def configured_client(tmp_path):
    import json

    cfg = tmp_path / "k8s.json"
    cfg.write_text(
        json.dumps({"server": "https://127.0.0.1:6443", "ca_data": "", "token": "secret-token"}),
        encoding="utf-8",
    )
    return ScopedKubernetesClient(config_path=str(cfg))


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.get_deployment("..", "checkout", "uid"),
        lambda client: client.get_deployment("demo", "..", "uid"),
        lambda client: client.get_deployment("", "checkout", "uid"),
        lambda client: client.list_owned_replicasets("..", "uid"),
        lambda client: client.list_namespace_events("..", {"uid"}),
        lambda client: client.get_pod_logs("demo", "..", "app"),
        lambda client: client.get_configmap_metadata("demo", ".."),
    ],
)
def test_client_rejects_path_identity_before_any_request(tmp_path, call):
    client = configured_client(tmp_path)
    calls = []

    client._request_json = lambda *args, **kwargs: (calls.append("json"), {})[1]
    client._request_text = lambda *args, **kwargs: (calls.append("text"), "")[1]
    with pytest.raises(ScopedK8sError) as exc:
        call(client)
    assert exc.value.code == "target_changed"
    assert calls == []


def test_client_quotes_identity_segments_into_one_path_segment(tmp_path):
    client = configured_client(tmp_path)
    paths = []

    def request_json(path, params=None):
        paths.append(path)
        return {
            "metadata": {
                "namespace": "demo/../kube-system",
                "name": "checkout?x=1#f",
                "uid": "uid",
            }
        }

    client._request_json = request_json
    client.get_deployment("demo/../kube-system", "checkout?x=1#f", expected_uid="uid")
    assert paths == [
        "/apis/apps/v1/namespaces/demo%2F..%2Fkube-system/deployments/checkout%3Fx%3D1%23f"
    ]


def test_configmap_read_quotes_name_inside_configmaps_path(tmp_path):
    client = configured_client(tmp_path)
    paths = []

    def request_json(path, params=None, **kwargs):
        paths.append(path)
        return {
            "kind": "PartialObjectMetadata",
            "apiVersion": "meta.k8s.io/v1",
            "metadata": {"uid": "cm", "name": "config/../../secrets", "namespace": "demo"},
        }

    client._request_json = request_json
    client.get_configmap_metadata("demo", "config/../../secrets")
    assert paths == ["/api/v1/namespaces/demo/configmaps/config%2F..%2F..%2Fsecrets"]


def test_pod_list_ignores_foreign_namespace_and_wrong_owner(tmp_path):
    client = configured_client(tmp_path)

    def request_json(path, params=None):
        return {
            "kind": "PodList",
            "items": [
                {
                    "metadata": {
                        "namespace": "demo",
                        "name": "owned",
                        "uid": "owned",
                        "ownerReferences": [
                            {
                                "uid": "rs-1",
                                "kind": "ReplicaSet",
                                "apiVersion": "apps/v1",
                                "controller": True,
                            }
                        ],
                    }
                },
                {
                    "metadata": {
                        "namespace": "kube-system",
                        "name": "foreign",
                        "uid": "foreign",
                        "ownerReferences": [
                            {
                                "uid": "rs-1",
                                "kind": "ReplicaSet",
                                "apiVersion": "apps/v1",
                                "controller": True,
                            }
                        ],
                    }
                },
                {
                    "metadata": {
                        "namespace": "demo",
                        "name": "wrong-owner",
                        "uid": "wrong-owner",
                        "ownerReferences": [
                            {
                                "uid": "rs-other",
                                "kind": "ReplicaSet",
                                "apiVersion": "apps/v1",
                                "controller": True,
                            }
                        ],
                    }
                },
            ],
        }

    client._request_json = request_json
    selected = client.list_owned_pods("demo", {"rs-1"})
    assert [item["metadata"]["uid"] for item in selected] == ["owned"]


def make_context(workload_uid="deploy-uid-1", immutable_window=None):
    return CollectionContext(
        run_id=uuid4(),
        incident_id=uuid4(),
        workload_id=uuid4(),
        cluster_id="local-k3s",
        namespace="demo",
        workload_uid=workload_uid,
        immutable_window=immutable_window
        or WindowSnapshot(
            datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
            "explicit",
        ),
        scope_snapshot=ScopeSnapshot(
            "local-k3s", "demo", "checkout", workload_uid, "live_kubernetes"
        ),
        configured_catalog_version="standard_v1",
        attempt_number=1,
        max_attempts=3,
    )


class FakeClient:
    """Replaces _scoped_client(); each test sets the method values it needs."""


def test_collect_kubernetes_state_success(monkeypatch):
    ctx = make_context()
    fake = FakeClient()
    fake.get_deployment = lambda ns, name, expected_uid: {
        "metadata": {"uid": expected_uid, "namespace": ns, "name": name}
    }
    fake.list_owned_replicasets = lambda ns, uid: []
    fake.list_owned_pods = lambda ns, rs_uids: []
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_state(ctx, timeout_seconds=5.0)
    assert res.source_key == "kubernetes.state"
    assert res.payload_kind == "state"
    assert res.coverage.status == "available"
    assert "deployment" in res.redacted_bounded_payload
    assert "pods" in res.redacted_bounded_payload


def test_state_translates_uid_drift_to_identity_changed(monkeypatch):
    ctx = make_context()
    fake = FakeClient()

    def drift(ns, name, expected_uid):
        raise ScopedK8sError("target_changed")

    fake.get_deployment = drift
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_state(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "unavailable"
    assert res.coverage.reason_codes == ["identity_changed"]


def test_state_translates_forbidden(monkeypatch):
    ctx = make_context()
    fake = FakeClient()

    def denied(ns, name, expected_uid):
        raise ScopedK8sError("forbidden")

    fake.get_deployment = denied
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_state(ctx, timeout_seconds=5.0)
    assert res.coverage.reason_codes == ["forbidden"]


def test_collect_kubernetes_events_filters_by_involved_uids(monkeypatch):
    ctx = make_context()
    fake = FakeClient()
    fake.get_deployment = lambda ns, name, expected_uid: {
        "metadata": {"uid": expected_uid, "name": name, "namespace": ns}
    }
    fake.list_owned_replicasets = lambda ns, deployment_uid: [
        {
            "metadata": {
                "uid": "rs-uid-1",
                "name": "checkout-abc",
                "namespace": ns,
                "ownerReferences": [
                    {
                        "uid": deployment_uid,
                        "kind": "Deployment",
                        "apiVersion": "apps/v1",
                        "controller": True,
                    }
                ],
            }
        }
    ]
    fake.list_owned_pods = lambda ns, rs_uids: [
        {
            "metadata": {
                "uid": "pod-uid-1",
                "name": "checkout-abc-123",
                "namespace": ns,
                "ownerReferences": [
                    {
                        "uid": "rs-uid-1",
                        "kind": "ReplicaSet",
                        "apiVersion": "apps/v1",
                        "controller": True,
                    }
                ],
            }
        }
    ]
    fake.list_namespace_events = lambda ns, involved_uids: [
        {
            "metadata": {"uid": "event-1", "name": "checkout.1", "namespace": ns},
            "involvedObject": {"uid": "pod-uid-1", "kind": "Pod", "namespace": ns},
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "type": "Warning",
            "count": 5,
            "lastTimestamp": "2026-01-01T00:00:10Z",
        },
        {
            "metadata": {"uid": "event-2", "name": "unrelated.1", "namespace": ns},
            "involvedObject": {"uid": "other-pod-uid", "kind": "Pod", "namespace": ns},
            "reason": "BackOff",
            "message": "unrelated",
            "type": "Warning",
        },
    ]
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_events(ctx, timeout_seconds=5.0)
    assert res.source_key == "kubernetes.events"
    assert res.payload_kind == "events"
    assert res.coverage.status == "available"
    events = res.redacted_bounded_payload["events"]
    assert len(events) == 1
    assert events[0]["involved_uid"] == "pod-uid-1"


def test_empty_events_is_available_not_absence_of_history(monkeypatch):
    ctx = make_context()
    fake = FakeClient()
    fake.get_deployment = lambda ns, name, expected_uid: {
        "metadata": {"uid": expected_uid, "name": name, "namespace": ns}
    }
    fake.list_owned_replicasets = lambda ns, deployment_uid: []
    fake.list_owned_pods = lambda ns, rs_uids: []
    fake.list_namespace_events = lambda ns, involved_uids: []
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_events(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "available"
    assert res.coverage.reason_codes == []
    assert res.redacted_bounded_payload == {"events": []}


def log_pod(*, restart_count=0, last_state=None, phase="Running", containers=None):
    containers = containers or [{"name": "app", "image": "fixture:1"}]
    statuses = []
    for container in containers:
        status = {"name": container["name"], "restartCount": restart_count}
        if phase == "Running":
            status["state"] = {"running": {"startedAt": "2026-01-01T00:00:00Z"}}
            status["started"] = True
        if restart_count:
            status["lastState"] = last_state or {"terminated": {"reason": "Error", "exitCode": 1}}
        statuses.append(status)
    return {
        "metadata": {
            "uid": "pod-uid-1",
            "name": "checkout-abc-123",
            "namespace": "demo",
            "ownerReferences": [
                {
                    "uid": "rs-uid-1",
                    "kind": "ReplicaSet",
                    "apiVersion": "apps/v1",
                    "controller": True,
                }
            ],
        },
        "spec": {"containers": containers},
        "status": {"phase": phase, "containerStatuses": statuses},
    }


def log_fake(ctx, pod, logs):
    fake = FakeClient()
    fake.get_deployment = lambda ns, name, expected_uid: {
        "metadata": {"uid": expected_uid, "name": name, "namespace": ns}
    }
    fake.list_owned_replicasets = lambda ns, deployment_uid: [
        {
            "metadata": {
                "uid": "rs-uid-1",
                "name": "checkout-abc",
                "namespace": ns,
                "ownerReferences": [
                    {
                        "uid": deployment_uid,
                        "kind": "Deployment",
                        "apiVersion": "apps/v1",
                        "controller": True,
                    }
                ],
            }
        }
    ]
    fake.list_owned_pods = lambda ns, rs_uids: [pod]
    fake.get_pod = lambda ns, name, expected_uid, rs_uids: pod
    fake.get_pod_logs = logs
    return fake


def test_logs_current_collects_bounded_lines(monkeypatch):
    ctx = make_context()
    fake = log_fake(
        ctx,
        log_pod(),
        lambda ns, pod, container, previous=False, since_time=None, line_limit=200: (
            "2026-01-01T00:00:00Z line\n",
            False,
        ),
    )
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_current(ctx, timeout_seconds=5.0)
    assert res.source_key == "kubernetes.logs.current"
    assert res.payload_kind == "logs"
    assert res.coverage.status == "available"
    assert len(res.redacted_bounded_payload["logs"]) > 0
    log_entry = res.redacted_bounded_payload["logs"][0]
    assert log_entry["previous"] is False
    assert "text" in log_entry


def test_logs_current_discloses_truncated_cap(monkeypatch):
    ctx = make_context()
    fake = log_fake(
        ctx,
        log_pod(),
        lambda ns, pod, container, previous=False, since_time=None, line_limit=200: (
            "2026-01-01T00:00:00Z line\n" * 200,
            True,
        ),
    )
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_current(ctx, timeout_seconds=5.0)
    assert res.coverage == Coverage(status="partial", reason_codes=["truncated"])
    assert res.redacted_bounded_payload["truncated"] is True


def test_logs_error_translations_match_state(monkeypatch):
    ctx = make_context()
    for code, published in (
        ("target_changed", "identity_changed"),
        ("forbidden", "forbidden"),
        ("cluster_unavailable", "source_unavailable"),
    ):
        fake = FakeClient()

        def raise_code(ns, name, expected_uid, code=code):
            raise ScopedK8sError(code)

        fake.get_deployment = raise_code
        monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
        res = collect_kubernetes_logs_current(ctx, timeout_seconds=5.0)
        assert res.coverage.status == "unavailable" and res.coverage.reason_codes == [published]


def test_logs_current_for_unschedulable_pending_container_is_not_applicable(monkeypatch):
    ctx = make_context()
    calls = []

    def logs(*args, **kwargs):
        # Real Kubernetes returns 200 with an empty body for an unschedulable
        # container that has not been created on a node yet.
        calls.append((args, kwargs))
        return "", False

    fake = log_fake(ctx, log_pod(phase="Pending", restart_count=0), logs)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_current(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "not_applicable"
    assert res.coverage.reason_codes == ["not_applicable"]
    assert res.redacted_bounded_payload is None
    assert calls == []


def test_logs_previous_when_container_not_restarted_is_not_applicable(monkeypatch):
    ctx = make_context()
    calls = []

    def logs(*args, **kwargs):
        calls.append((args, kwargs))
        return "unexpected\n", False

    fake = log_fake(ctx, log_pod(restart_count=0), logs)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_previous(ctx, timeout_seconds=5.0)
    assert res.source_key == "kubernetes.logs.previous"
    assert res.payload_kind == "logs"
    assert res.coverage.status == "not_applicable"
    assert res.coverage.reason_codes == ["not_applicable"]
    assert calls == []


def test_logs_previous_when_restarted_collects_previous_logs(monkeypatch):
    ctx = make_context()
    fake = log_fake(
        ctx,
        log_pod(restart_count=2),
        lambda ns, pod, container, previous=False, since_time=None, line_limit=200: (
            "2025-12-31T23:59:59Z previous stacktrace\n",
            False,
        ),
    )
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_previous(ctx, timeout_seconds=5.0)
    assert res.source_key == "kubernetes.logs.previous"
    assert res.payload_kind == "logs"
    assert res.coverage.status == "available"
    assert res.redacted_bounded_payload["logs"][0]["previous"] is True


def test_previous_logs_allow_a_proven_no_restart_sidecar(monkeypatch):
    ctx = make_context()
    containers = [{"name": "app", "image": "fixture:1"}, {"name": "sidecar", "image": "x"}]
    pod = log_pod(restart_count=2, containers=containers)
    # Container app restarted; sidecar never did and a previous read would 400.
    pod["status"]["containerStatuses"][1] = {"name": "sidecar", "restartCount": 0}

    def logs(ns, pod_name, container, previous=False, since_time=None, line_limit=200):
        if container == "sidecar":
            raise ScopedK8sError("container_not_ready")
        return "2025-12-31T23:59:59Z previous stacktrace\n", False

    fake = log_fake(ctx, pod, logs)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_previous(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "available"
    assert any(c.get("previous") == "available" for c in res.redacted_bounded_payload["containers"])


def test_missing_previous_logs_remain_a_required_gap(monkeypatch):
    ctx = make_context()
    fake = log_fake(
        ctx,
        log_pod(restart_count=2),
        lambda ns, pod, container, previous=False, since_time=None, line_limit=200: (
            _raise(ScopedK8sError("container_not_ready"))
        ),
    )
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_previous(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "unavailable"
    assert res.coverage.reason_codes == ["no_samples"]


def _raise(error):
    raise error


def test_configmap_metadata_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("KUBE_CONFIGMAP_ENABLED", raising=False)
    ctx = make_context()
    calls = []
    fake = FakeClient()
    fake.get_deployment = lambda *args, **kwargs: calls.append(args)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = kubernetes_collector.collect_kubernetes_configmap_metadata(ctx, timeout_seconds=5.0)
    assert res.coverage.status == "unavailable"
    assert res.coverage.reason_codes == ["source_not_configured"]
    assert res.redacted_bounded_payload is None
    assert calls == []


def test_logs_disclose_extends_after_window(monkeypatch):
    window = WindowSnapshot(
        start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
        source="explicit",
    )
    ctx = make_context(immutable_window=window)
    fake = log_fake(
        ctx,
        log_pod(),
        lambda ns, pod, container, previous=False, since_time=None, line_limit=200: (
            "2026-01-01T00:00:00Z line after window\n",
            False,
        ),
    )
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    res = collect_kubernetes_logs_current(ctx, timeout_seconds=5.0)
    assert res.source_times.time_relation == "extends_after_window"
