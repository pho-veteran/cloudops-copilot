"""Contract tests for the four Kubernetes incident families and evidence bounds."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from conftest import sign_in
from sqlalchemy import update
from test_access_admission import create, incident_body

from cloudops.features.investigations import collector, kubernetes_collector
from cloudops.features.investigations.collector import prepare
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.schemas import Coverage
from cloudops.features.investigations.storage import store_batch
from cloudops.shared.database import sessions


def new_live_context(client):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    run_id = UUID(created["run_id"])
    with sessions()() as db, db.begin():
        row = db.get(InvestigationRun, run_id)
        row.window_start = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
        row.window_end = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        row.window_source = "explicit"
    context = prepare(run_id, attempt_number=1, max_attempts=3)
    return run_id, context


def set_window(run_id, start, end):
    with sessions()() as db, db.begin():
        db.execute(
            update(InvestigationRun)
            .where(InvestigationRun.id == run_id)
            .values(window_start=start, window_end=end, window_source="explicit")
        )
    with sessions()() as db:
        return prepare(run_id, attempt_number=1, max_attempts=3)


def collect_and_store(client, monkeypatch, fake, *, context=None, override=None):
    run_id, context = new_live_context(client) if context is None else (context[0], context[1])
    if context is None:
        raise AssertionError
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    results = list(
        collector.collect(context, collector.configured_adapters("live_kubernetes")).source_results
    )
    if override is not None:
        results = override(results)
    with sessions()() as db:
        rows = store_batch(db, run_id, context.workload_uid, results)
        db.commit()
        by_key = {row.source_key: row for row in rows}
        run = db.get(InvestigationRun, run_id)
        snapshot = SimpleNamespace(
            run_id=run_id,
            context=context,
            rows=by_key,
            pack=run.pack_coverage,
            packs={item["pack"]: item for item in run.pack_coverage},
            coverage={key: row.coverage for key, row in by_key.items()},
            payloads={key: row.payload for key, row in by_key.items()},
            digests={key: row.sha256 for key, row in by_key.items()},
            times={key: row.time_relation for key, row in by_key.items()},
            windows={key: row.requested_window for key, row in by_key.items()},
        )
    return snapshot


def deployment(uid):
    return {
        "metadata": {"uid": uid, "name": "checkout", "namespace": "demo", "generation": 3},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "app", "image": "fixture:1"}]}},
        },
        "status": {"readyReplicas": 0, "availableReplicas": 0},
    }


def pod(uid, *, phase="Running", restart_count=0, state=None, last_state=None, namespace="demo"):
    if state is None and phase == "Running":
        state = {"running": {"startedAt": "2026-01-01T00:00:00Z"}}
    status = {
        "phase": phase,
        "containerStatuses": [
            {
                "name": "app",
                "restartCount": restart_count,
                "state": state or {},
                "lastState": last_state or {},
                "started": phase == "Running",
            }
        ],
    }
    if phase == "Pending":
        status["conditions"] = [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
        ]
    return {
        "metadata": {
            "uid": uid,
            "name": "checkout-abc-123",
            "namespace": namespace,
            "ownerReferences": [
                {
                    "uid": "rs-uid-1",
                    "kind": "ReplicaSet",
                    "apiVersion": "apps/v1",
                    "controller": True,
                }
            ],
        },
        "spec": {"containers": [{"name": "app", "image": "fixture:1"}]},
        "status": status,
    }


def event(uid, reason, message):
    return {
        "metadata": {"uid": "event-1", "name": "checkout.1", "namespace": "demo"},
        "involvedObject": {"uid": uid, "kind": "Pod", "namespace": "demo"},
        "type": "Warning",
        "reason": reason,
        "message": message,
        "count": 4,
        "lastTimestamp": "2026-01-01T00:00:10Z",
    }


class FakeCluster:
    """One read-only scoped client stand-in shared by every adapter."""

    def __init__(
        self,
        *,
        pods=None,
        events=None,
        current="2026-01-01T00:00:00Z current line\n",
        previous="2025-12-31T23:59:59Z previous line\n",
        drift_at=None,
        denied=False,
        configmap_kind="ConfigMap",
        current_truncated=False,
        previous_truncated=False,
    ):
        self.pods = pods or [pod("pod-uid-1")]
        self.events = events or []
        self.current = current
        self.previous = previous
        self.drift_at = drift_at
        self.denied = denied
        self.configmap_kind = configmap_kind
        self.current_truncated = current_truncated
        self.previous_truncated = previous_truncated
        self.deployment_reads = 0

    def _check(self):
        if self.denied:
            raise kubernetes_collector.ScopedK8sError("forbidden")

    def get_deployment(self, namespace, name, expected_uid):
        self._check()
        self.deployment_reads += 1
        if self.drift_at is not None and self.deployment_reads == self.drift_at:
            return {"metadata": {"uid": "replacement-uid", "name": name, "namespace": namespace}}
        return deployment(expected_uid)

    def list_owned_replicasets(self, namespace, deployment_uid):
        self._check()
        return [
            {
                "metadata": {
                    "uid": "rs-uid-1",
                    "name": "checkout-abc",
                    "namespace": namespace,
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

    def list_owned_pods(self, namespace, replicaset_uids):
        self._check()
        return list(self.pods)

    def list_namespace_events(self, namespace, involved_uids):
        self._check()
        return list(self.events)

    def get_pod(self, namespace, name, expected_uid, rs_uids):
        for value in self.pods:
            metadata = value["metadata"]
            if (
                metadata["namespace"] == namespace
                and metadata["name"] == name
                and metadata["uid"] == expected_uid
            ):
                return value
        raise kubernetes_collector.ScopedK8sError("target_changed")

    def get_pod_logs(
        self, namespace, pod_name, container_name, previous=False, since_time=None, line_limit=200
    ):
        self._check()
        text = self.previous if previous else self.current
        if text is None:
            raise kubernetes_collector.ScopedK8sError("container_not_ready")
        return text, self.previous_truncated if previous else self.current_truncated

    def get_configmap_metadata(self, namespace, name):
        self._check()
        return {
            "kind": self.configmap_kind,
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": "cm-uid",
                "resourceVersion": "7",
            },
            "data": {"password": "sentinel"},
        }


def test_crashloop_family_is_available_with_observable_pack(client, monkeypatch):
    fake = FakeCluster(
        pods=[
            pod(
                "pod-uid-1",
                restart_count=5,
                state={"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                last_state={"terminated": {"reason": "Error", "exitCode": 1}},
            ),
            # Cross-namespace injection and a foreign owner must never be published.
            pod("pod-other-ns", namespace="other"),
            {
                **pod("pod-wrong-owner"),
                "metadata": {
                    **pod("pod-wrong-owner")["metadata"],
                    "ownerReferences": [
                        {
                            "uid": "rs-other",
                            "kind": "ReplicaSet",
                            "apiVersion": "apps/v1",
                            "controller": True,
                        }
                    ],
                },
            },
        ],
        events=[event("pod-uid-1", "BackOff", "Back-off restarting failed container")],
        current="2026-01-01T00:00:00Z startup failed\n",
        previous="2025-12-31T23:59:59Z previous stacktrace\n",
    )
    result = collect_and_store(client, monkeypatch, fake)
    assert result.coverage["kubernetes.state"]["status"] == "available"
    assert [item["uid"] for item in result.payloads["kubernetes.state"]["pods"]] == ["pod-uid-1"]
    assert result.packs["crashloop_startup"]["status"] == "available"
    assert result.packs["crashloop_startup"]["missing_required"] == []
    assert "prometheus.restarts" in result.packs["crashloop_startup"]["optional_gaps"]
    assert "kubernetes.configmap.metadata" in result.packs["crashloop_startup"]["optional_gaps"]


def test_oom_family_is_available(client, monkeypatch):
    fake = FakeCluster(
        pods=[
            pod(
                "pod-uid-1",
                restart_count=3,
                state={"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            )
        ],
        events=[event("pod-uid-1", "OOMKilled", "container was OOM killed")],
    )
    result = collect_and_store(client, monkeypatch, fake)
    assert result.packs["oom_restarts"]["status"] == "available"


def test_pending_family_is_available_with_conditional_logs(client, monkeypatch):
    fake = FakeCluster(
        pods=[pod("pod-uid-1", phase="Pending", restart_count=0)],
        events=[
            event(
                "pod-uid-1",
                "FailedScheduling",
                "0/1 nodes are available: insufficient cpu",
            )
        ],
        current=None,
        previous=None,
    )
    result = collect_and_store(client, monkeypatch, fake)
    assert result.coverage["kubernetes.logs.current"]["status"] == "not_applicable"
    assert result.coverage["kubernetes.logs.previous"]["status"] == "not_applicable"
    assert result.packs["pending_unschedulable"]["status"] == "available"


def test_image_pull_family_is_available(client, monkeypatch):
    fake = FakeCluster(
        pods=[
            pod(
                "pod-uid-1",
                restart_count=0,
                state={"waiting": {"reason": "ImagePullBackOff", "message": "pull back-off"}},
            )
        ],
        events=[event("pod-uid-1", "Failed", "Failed to pull image")],
        current=None,
        previous=None,
    )
    result = collect_and_store(client, monkeypatch, fake)
    assert result.packs["image_pull"]["status"] == "available"


def test_redaction_happens_before_persistence_and_hashing(client, monkeypatch):
    fake = FakeCluster(
        events=[event("pod-uid-1", "Warning", "token=secret-token-sentinel")],
        current=(
            "2026-01-01T00:00:00Z Authorization: Bearer secret-token-sentinel\n"
            "2026-01-01T00:00:01Z password=password-sentinel\n"
        ),
    )
    result = collect_and_store(client, monkeypatch, fake)
    stored = repr(result.payloads)
    assert "[REDACTED]" in stored
    assert "secret-token-sentinel" not in stored
    assert "password-sentinel" not in stored
    assert result.digests["kubernetes.logs.current"]


def test_denied_source_is_published_without_error_text(client, monkeypatch):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    result = collect_and_store(client, monkeypatch, FakeCluster(denied=True))
    for key in (
        "kubernetes.state",
        "kubernetes.events",
        "kubernetes.logs.current",
        "kubernetes.logs.previous",
        "kubernetes.configmap.metadata",
    ):
        assert result.coverage[key] == {"status": "unavailable", "reason_codes": ["forbidden"]}
        assert result.payloads[key] is None
    stored = repr(result.payloads) + repr(result.coverage)
    assert "Bearer" not in stored and "secret-token" not in stored and "https://" not in stored
    assert result.coverage["prometheus.request_rate"]["reason_codes"] == ["source_not_configured"]


def test_stale_window_is_disclosed_and_history_is_not_rewritten(client, monkeypatch):
    run_id, _ = new_live_context(client)
    old_end = datetime.now(timezone.utc) - timedelta(hours=1)
    old_start = old_end - timedelta(minutes=30)
    context = set_window(run_id, old_start, old_end)
    # Restarted containers so the previous-log slot is genuinely available;
    # staleness, not conditional log availability, is what this test pins.
    fake = FakeCluster(pods=[pod("pod-uid-1", restart_count=2)])
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    results = list(
        collector.collect(context, collector.configured_adapters("live_kubernetes")).source_results
    )
    with sessions()() as db:
        rows = {
            row.source_key: row for row in store_batch(db, run_id, context.workload_uid, results)
        }
        db.commit()
        for key in (
            "kubernetes.state",
            "kubernetes.events",
            "kubernetes.logs.current",
            "kubernetes.logs.previous",
        ):
            row = rows[key]
            if key.startswith("kubernetes.logs"):
                assert row.time_relation == "not_captured"
                assert row.coverage["status"] == "partial"
                assert "stale" in row.coverage["reason_codes"]
            else:
                assert row.time_relation == "extends_after_window"
                assert row.coverage["status"] == "available"
            assert row.requested_window["end"] == old_end.isoformat()
            assert row.requested_window["end"] != datetime.now(timezone.utc).isoformat()


def test_truncated_logs_persist_partial_coverage_and_payload_flag(client, monkeypatch):
    fake = FakeCluster(
        current="2026-01-01T00:00:00Z line\n" * 200,
        current_truncated=True,
    )
    result = collect_and_store(client, monkeypatch, fake)
    assert result.coverage["kubernetes.logs.current"] == {
        "status": "partial",
        "reason_codes": ["truncated"],
    }
    assert result.payloads["kubernetes.logs.current"]["truncated"] is True
    assert result.packs["crashloop_startup"]["status"] == "partial"
    assert "kubernetes.logs.current" in result.packs["crashloop_startup"]["missing_required"]


def test_stale_reason_round_trips_and_unknown_reason_still_fails(client, monkeypatch):
    fake = FakeCluster()

    def stale(results):
        return [
            result.model_copy(
                update={"coverage": Coverage(status="unavailable", reason_codes=["stale"])}
            )
            if result.source_key == "kubernetes.configmap.metadata"
            else result
            for result in results
        ]

    result = collect_and_store(client, monkeypatch, fake, override=stale)
    assert result.coverage["kubernetes.configmap.metadata"]["reason_codes"] == ["stale"]

    def unknown(results):
        return [
            result.model_copy(
                update={
                    "coverage": Coverage(status="unavailable", reason_codes=["definitely_unknown"])
                }
            )
            if result.source_key == "kubernetes.configmap.metadata"
            else result
            for result in results
        ]

    run_id, context = new_live_context(client)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: FakeCluster())
    results = list(
        collector.collect(context, collector.configured_adapters("live_kubernetes")).source_results
    )
    with sessions()() as db, pytest.raises(ValueError):
        store_batch(db, run_id, context.workload_uid, unknown(results))


def test_bracket_uid_drift_fails_closed_with_identity_changed(client, monkeypatch):
    fake = FakeCluster(drift_at=2)
    result = collect_and_store(client, monkeypatch, fake)
    assert result.coverage["kubernetes.state"] == {
        "status": "unavailable",
        "reason_codes": ["identity_changed"],
    }
    assert result.payloads["kubernetes.state"] is None
    assert result.digests["kubernetes.state"] is None
    assert "target_changed" not in repr(result.coverage)


def test_template_and_init_state_survive_safe_storage(client, monkeypatch):
    value = pod("pod-uid-1")
    value["spec"]["initContainers"] = [{"name": "setup", "image": "init:2"}]
    value["status"]["initContainerStatuses"] = [
        {
            "name": "setup",
            "restartCount": 1,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"exitCode": 1}},
        }
    ]
    fake = FakeCluster(pods=[value])
    original = fake.get_deployment

    def desired(*args, **kwargs):
        result = original(*args, **kwargs)
        result["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "2"}
        result["spec"]["template"]["spec"]["containers"][0].update(
            image="desired:2",
            env=[{"name": "API_TOKEN", "value": "private-literal-sentinel"}],
            resources={"requests": {"cpu": "10m"}, "limits": {"memory": "32Mi"}},
        )
        return result

    fake.get_deployment = desired
    result = collect_and_store(client, monkeypatch, fake)
    payload = result.payloads["kubernetes.state"]
    assert payload["deployment"]["revision"] == "2"
    template = payload["deployment"]["template"]["containers"][0]
    assert template["image"] == "desired:2"
    assert template["resources"]["limits"]["memory"] == "32Mi"
    assert (
        next(row for row in payload["pods"][0]["containers"] if row["name"] == "setup")["kind"]
        == "init"
    )
    assert "private-literal-sentinel" not in repr(result.payloads)


def test_windowless_legacy_run_finishes_through_checkpointed_worker(client, monkeypatch):
    from cloudops.entrypoints.worker import run_once
    from cloudops.shared.config import settings
    from cloudops.shared.jobs import Job
    from sqlalchemy import select

    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        run.window_start = run.window_end = None
        run.window_source = "not_captured"
    calls = []
    monkeypatch.setattr(
        kubernetes_collector, "_scoped_client", lambda *_: calls.append(True) or FakeCluster()
    )
    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "live_kubernetes")
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    settings.cache_clear()
    assert run_once()
    with sessions()() as db:
        assert db.scalar(select(Job.state)) == "completed"
    run = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert run["state"] == "insufficient_evidence"
    assert all(
        row["coverage"]["reason_codes"] == ["not_captured"]
        for row in run["evidence"]
        if row["source_key"].startswith("kubernetes.")
    )
    assert calls == []
