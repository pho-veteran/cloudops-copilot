from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cloudops.features.investigations import collector
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES, derive_pack_coverage
from cloudops.features.investigations.schemas import Coverage


def context(attempt=1, maximum=3):
    return collector.CollectionContext(
        run_id=uuid4(),
        incident_id=uuid4(),
        workload_id=uuid4(),
        cluster_id="cluster",
        namespace="namespace",
        workload_uid="uid",
        immutable_window=None,
        scope_snapshot=collector.ScopeSnapshot(
            "cluster", "namespace", "app", "uid", "development_fixture"
        ),
        configured_catalog_version=CATALOG_VERSION,
        attempt_number=attempt,
        max_attempts=maximum,
    )


def test_fixture_is_explicit_and_never_counts_as_live_coverage(monkeypatch):
    monkeypatch.setattr(
        collector,
        "settings",
        lambda: SimpleNamespace(collector_delay_seconds=0, collector_fail=False),
    )
    result = collector.collect(context(), collector.configured_adapters("development_fixture"))
    assert len(result.source_results) == len(SOURCES)
    outcomes = {source.source_key: source.coverage for source in result.source_results}
    assert outcomes["development_fixture"].status == "available"
    assert all(
        outcomes[key].reason_codes == ["source_not_configured"]
        for key in SOURCES
        if key != "development_fixture"
    )
    assert all(pack["status"] == "unavailable" for pack in derive_pack_coverage(outcomes))
    fixture = result.source_results[-1]
    assert fixture.redacted_bounded_payload["workload"] == "cluster/namespace/app"
    assert fixture.source_times.observed_at <= datetime.now(timezone.utc)


def test_pack_coverage_conditional_logs_and_optional_gaps():
    available = Coverage(status="available", reason_codes=[])
    na = Coverage(status="not_applicable", reason_codes=["not_applicable"])
    outcomes = {
        "kubernetes.state": available,
        "kubernetes.events": available,
        "kubernetes.logs.current": available,
        "kubernetes.logs.previous": na,
    }
    packs = {pack["pack"]: pack for pack in derive_pack_coverage(outcomes)}
    assert packs["crashloop_startup"]["status"] == "available"
    assert "prometheus.restarts" in packs["crashloop_startup"]["optional_gaps"]
    assert packs["traffic_latency_errors"]["status"] == "unavailable"
    outcomes["kubernetes.events"] = Coverage(status="partial", reason_codes=["truncated"])
    packs = {pack["pack"]: pack for pack in derive_pack_coverage(outcomes)}
    assert packs["crashloop_startup"]["status"] == "partial"
    assert packs["crashloop_startup"]["missing_required"] == ["kubernetes.events"]


def test_configured_adapters_supports_live_kubernetes():
    adapters = collector.configured_adapters("live_kubernetes")
    assert "kubernetes.state" in adapters
    assert "kubernetes.events" in adapters
    assert "kubernetes.logs.current" in adapters
    assert "kubernetes.logs.previous" in adapters
    assert "kubernetes.configmap.metadata" in adapters
    assert "development_fixture" not in adapters


def test_configmap_metadata_returns_metadata_only_and_refuses_secrets(monkeypatch):
    from cloudops.features.investigations import kubernetes_collector as kube

    ctx = context()
    ctx = collector.CollectionContext(
        run_id=ctx.run_id,
        incident_id=ctx.incident_id,
        workload_id=ctx.workload_id,
        cluster_id=ctx.cluster_id,
        namespace=ctx.namespace,
        workload_uid=ctx.workload_uid,
        immutable_window=collector.WindowSnapshot(
            datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
            "explicit",
        ),
        scope_snapshot=collector.ScopeSnapshot(
            ctx.cluster_id, ctx.namespace, "app", ctx.workload_uid, "live_kubernetes"
        ),
        configured_catalog_version=CATALOG_VERSION,
        attempt_number=1,
        max_attempts=3,
    )

    class FakeClient:
        def get_deployment(self, namespace, name, expected_uid):
            return {
                "metadata": {"uid": expected_uid, "name": name, "namespace": namespace},
                "spec": {
                    "template": {
                        "spec": {"containers": [{"envFrom": [{"configMapRef": {"name": "app"}}]}]}
                    }
                },
            }

        def get_configmap_metadata(self, namespace, name):
            return {
                "kind": "PartialObjectMetadata",
                "apiVersion": "meta.k8s.io/v1",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": "cm-uid",
                    "resourceVersion": "7",
                },
            }

    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    monkeypatch.setattr(kube, "_scoped_client", lambda _deadline: FakeClient())
    result = kube.collect_kubernetes_configmap_metadata(ctx, timeout_seconds=5.0)
    assert result.coverage.status == "available"
    assert result.redacted_bounded_payload["configmaps"] == [
        {"name": "app", "namespace": "namespace", "uid": "cm-uid", "resource_version": "7"}
    ]
    assert "sentinel" not in repr(result.redacted_bounded_payload)

    class SecretClient(FakeClient):
        def get_configmap_metadata(self, namespace, name):
            return {"kind": "Secret", "metadata": {"name": name, "uid": "secret-uid"}}

    monkeypatch.setattr(kube, "_scoped_client", lambda _deadline: SecretClient())
    denied = kube.collect_kubernetes_configmap_metadata(ctx, timeout_seconds=5.0)
    assert denied.coverage.status == "unavailable"
    assert denied.coverage.reason_codes == ["forbidden"]


def test_unknown_mode_and_unregistered_binding_fail_closed():
    with pytest.raises(ValueError):
        collector.configured_adapters("live")
    with pytest.raises(ValueError):
        collector.collect(context(), {"kubernetes.state": lambda *_: None})
    with pytest.raises(ValueError):
        collector.collect(context(), {"unknown": collector.development_fixture})


def test_transient_retries_shared_attempt_then_final_batch(monkeypatch):
    timeouts = []

    def failing(_context, timeout):
        timeouts.append(timeout)
        raise collector.SourceTimeout()

    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, "kubernetes.state", failing)
    with pytest.raises(collector.SourceTimeout):
        collector.collect(context(attempt=2), {"kubernetes.state": failing})
    final = collector.collect(context(attempt=3), {"kubernetes.state": failing})
    assert len(final.source_results) == len(SOURCES)
    assert final.source_results[0].coverage.reason_codes == ["timeout"]
    assert all(0 < timeout <= 5 for timeout in timeouts)


def test_negative_outcome_is_not_retried(monkeypatch):
    def unavailable(_context, _timeout):
        return collector._unavailable("kubernetes.state", "source_not_configured")

    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, "kubernetes.state", unavailable)
    result = collector.collect(context(), {"kubernetes.state": unavailable})
    assert result.source_results[0].coverage.reason_codes == ["source_not_configured"]


def test_attempt_deadline_turns_remaining_read_into_final_timeout(monkeypatch):
    clock = iter((0.0, 91.0))
    monkeypatch.setattr(collector.time, "monotonic", lambda: next(clock))
    called = []

    def adapter(_context, _timeout):
        called.append(True)

    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, "kubernetes.state", adapter)
    result = collector.collect(context(attempt=3), {"kubernetes.state": adapter})
    assert not called
    assert result.source_results[0].coverage.reason_codes == ["timeout"]


def test_final_source_timeout_uses_persisted_two_attempt_budget(client, monkeypatch):
    from conftest import sign_in
    from sqlalchemy import update
    from test_access_admission import create, incident_body

    from cloudops.entrypoints.worker import run_once
    from cloudops.shared.database import sessions
    from cloudops.shared.jobs import Job

    def timeout(_context, _deadline):
        raise collector.SourceTimeout()

    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "development_fixture")
    monkeypatch.setitem(collector.REGISTERED_ADAPTERS, "development_fixture", timeout)
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    with sessions()() as db, db.begin():
        db.execute(update(Job).where(Job.subject_id == ids["run_id"]).values(max_attempts=2))
    assert run_once() and run_once()
    result = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert (result["state"], result["outcome_code"]) == (
        "insufficient_evidence",
        "analysis_unavailable",
    )
    assert [(attempt["number"], attempt["state"]) for attempt in result["attempts"]] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    fixture = next(row for row in result["evidence"] if row["source_key"] == "development_fixture")
    assert fixture["coverage"] == {"status": "unavailable", "reason_codes": ["timeout"]}
    assert fixture["sha256"] is None and fixture["payload_ref"] is None
