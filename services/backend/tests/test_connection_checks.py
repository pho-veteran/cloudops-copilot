import asyncio
import base64
import hashlib
import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import certifi
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from cloudops.entrypoints import worker
from cloudops.entrypoints.api import app, create_app
from cloudops.features.access.models import LoginSession
from cloudops.features.access.service import provision
from cloudops.features.connections import service, sources
from cloudops.features.connections.models import ConnectionCheck
from cloudops.features.investigations import prometheus
from cloudops.features.investigations.prometheus_config import PrometheusConfig
from cloudops.features.workloads import observation_reader
from cloudops.features.workloads.models import ScopeState, ScopeTarget, Workload
from cloudops.shared.cluster_identity import cluster_fingerprint
from cloudops.shared.config import settings
from cloudops.shared.database import BACKEND_ROOT, database_now, sessions
from conftest import sign_in

CHECKS = "/api/connection-checks"


@pytest.fixture
def configured(client, monkeypatch, tmp_path):
    der = ssl.create_default_context(cafile=certifi.where()).get_ca_certs(binary_form=True)[0]
    ca = base64.b64encode(ssl.DER_cert_to_PEM_cert(der).encode()).decode()
    kube = tmp_path / "kube.json"
    kube.write_text(
        json.dumps({"server": "https://kube.test", "ca_data": ca, "token": "scoped-kube"})
    )
    monkeypatch.setenv("KUBE_READ_CONFIG", str(kube))
    prom = tmp_path / "prom.json"
    from cloudops.features.investigations.prometheus_config import QUERY_IDS

    prom.write_text(
        json.dumps(
            {
                "schema": "PrometheusReadConfig/v1",
                "server": "https://prom.test",
                "endpoint_id": "lab",
                "producer_version": "test",
                "ca_data": ca,
                "token": "scoped-prom",
                "queries": {
                    key: {
                        "query": 'value{namespace="{{namespace}}",deployment_uid="{{workload_uid}}"}',
                        "unit": "bytes",
                    }
                    for key in QUERY_IDS
                },
            }
        )
    )
    monkeypatch.setenv("PROMETHEUS_READ_CONFIG", str(prom))
    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "prometheus")
    with sessions()() as db, db.begin():
        db.add(
            Workload(
                cluster="owned",
                namespace="demo",
                name="checkout",
                uid="uid-1",
                source="live_kubernetes",
            )
        )
        db.add(
            Workload(
                id=UUID(int=2**128 - 1),
                cluster="owned",
                namespace="demo",
                name="other",
                uid="other",
                source="live_kubernetes",
            )
        )
    return client, kube, prom


def route(monkeypatch, handler):
    read = sources.read

    async def fake(selection, started_at, deadline):
        return await read(selection, started_at, deadline, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(sources, "read", fake)


def post(client, auth, component, key=None):
    headers = {**auth, **({"Idempotency-Key": str(key)} if key else {})}
    return client.post(CHECKS, json={"component": component}, headers=headers)


def status(client, component):
    response = client.get(f"{CHECKS}/status")
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["component"] == component)


def allow_next(component):
    with sessions()() as db, db.begin():
        db.execute(
            update(ConnectionCheck)
            .where(ConnectionCheck.component == component)
            .values(
                attempted_at=ConnectionCheck.attempted_at - timedelta(seconds=61),
            )
        )


def streamed_json(payload):
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))


def deployment(request, uid="uid-1"):
    assert request.method == "GET"
    assert request.url.path == "/apis/apps/v1/namespaces/demo/deployments/checkout"
    assert request.headers["authorization"] == "Bearer scoped-kube"
    return streamed_json(
        {
            "kind": "Deployment",
            "apiVersion": "apps/v1",
            "metadata": {
                "name": "checkout",
                "namespace": "demo",
                "uid": uid,
            },
        },
    )


def test_admission_replay_is_durable_and_provider_does_not_hold_transaction(
    configured, monkeypatch
):
    client, _, _ = configured
    headers = sign_in(client)
    second = TestClient(app)
    with sessions()() as db:
        provision(db, "second", "local-test-password", "operator")
    other = sign_in(second, "second")
    entered, release = Event(), Event()
    calls = []

    def serve(request):
        calls.append(request.url)
        with sessions()() as db, db.begin():
            assert db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": service.ADMISSION_LOCK}
            )
            assert db.scalar(select(func.count()).select_from(ConnectionCheck)) == 1
        entered.set()
        assert release.wait(5)
        return deployment(request)

    route(monkeypatch, serve)
    key = uuid4()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post, client, headers, "cluster", key)
        try:
            assert entered.wait(5)
            pending = post(client, headers, "cluster", key).json()
            assert pending["reason_code"] == "check_pending"
            assert post(second, other, "cluster").json()["error"]["code"] == "check_in_progress"
            assert (
                post(second, other, "cluster", key).json()["error"]["code"]
                == "idempotency_conflict"
            )
            assert (
                post(client, headers, "database", key).json()["error"]["code"]
                == "idempotency_conflict"
            )
            assert post(second, other, "database").status_code == 201
        finally:
            release.set()
        finished = future.result(timeout=6).json()
    assert finished["id"] == pending["id"]
    assert finished["reason_code"] == "deployment_read"
    assert finished["evidence_at"] > finished["attempted_at"]
    assert post(client, headers, "cluster", key).json() == finished
    assert post(second, other, "cluster").json()["error"]["code"] == "check_cooldown"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (401, "permission_denied"),
        (403, "permission_denied"),
        (302, "redirect_refused"),
        (200, "invalid_response"),
    ],
)
@pytest.mark.parametrize("component", ["cluster", "prometheus"])
def test_failed_source_is_classified_and_never_redirected(
    configured, monkeypatch, component, code, reason
):
    client, _, _ = configured
    headers = sign_in(client)
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(
            code,
            headers={"Location": "https://redirect.test/sink"},
            stream=httpx.ByteStream(b"malformed"),
        )

    route(monkeypatch, serve)
    result = post(client, headers, component)
    assert result.status_code == 201
    assert result.json()["reason_code"] == reason
    assert len(requests) == 1
    assert status(client, component)["reason_code"] == reason


@pytest.mark.parametrize(
    ("shape", "reason", "coverage"),
    [
        ("full", "query_succeeded", "available"),
        ("empty", "metrics_missing", "missing"),
        ("foreign", "metrics_missing", "missing"),
        ("gapped", "metrics_partial", "partial"),
        ("stale", "metrics_stale", "partial"),
    ],
)
def test_metric_coverage_and_raw_freshness_are_independent(
    configured, monkeypatch, shape, reason, coverage
):
    client, _, _ = configured
    headers = sign_in(client)

    def serve(request):
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["query"] == 'value{namespace="demo",deployment_uid="uid-1"}'
        assert request.headers["authorization"] == "Bearer scoped-prom"
        start = float(request.url.params["start"])
        points = {"full": [0, 1, 2, 3, 4], "gapped": [0, 2, 4], "stale": [0, 1]}.get(shape, [0, 4])
        return streamed_json(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": []
                    if shape == "empty"
                    else [
                        {
                            "metric": {
                                "namespace": "demo",
                                "deployment_uid": "foreign" if shape == "foreign" else "uid-1",
                            },
                            "values": [[start + 30 * step, "0"] for step in points],
                        }
                    ],
                },
            },
        )

    route(monkeypatch, serve)
    row = post(client, headers, "prometheus").json()
    assert row["reason_code"] == reason
    assert row["metrics"]["coverage"] == coverage
    assert row["metrics"]["raw_freshness"] == "unknown"
    assert status(client, "prometheus")["metrics"] == row["metrics"]


def test_freshness_newer_denial_and_rotated_configuration(configured, monkeypatch):
    client, kube, _ = configured
    headers = sign_in(client)
    results = iter([None, 403])
    route(
        monkeypatch,
        lambda request: httpx.Response(code) if (code := next(results)) else deployment(request),
    )
    success = post(client, headers, "cluster").json()
    assert status(client, "cluster")["status"] == "available"
    with sessions()() as db, db.begin():
        row = db.get(ConnectionCheck, UUID(success["id"]))
        row.evidence_at = database_now(db) - timedelta(seconds=301)
    assert status(client, "cluster")["status"] == "stale"
    allow_next("cluster")
    denied = post(client, headers, "cluster").json()
    current = status(client, "cluster")
    assert current["status"] == "denied" and current["last_success_at"]
    assert current["last_failure_at"] == denied["finished_at"]
    config = json.loads(kube.read_text())
    config["token"] = "rotated"
    kube.write_text(json.dumps(config))
    assert status(client, "cluster")["reason_code"] == "binding_changed"
    assert all(receipt["historical"] for receipt in client.get(CHECKS).json())


def test_scope_and_provider_configuration_fail_without_io(configured, monkeypatch):
    client, kube, prom = configured
    headers = sign_in(client)
    route(monkeypatch, lambda request: pytest.fail("Unconfigured sources must make zero requests"))
    kube.unlink()
    prom.write_text("invalid")
    assert post(client, headers, "cluster").json()["reason_code"] == "config_missing"
    assert post(client, headers, "prometheus").json()["reason_code"] == "config_invalid"
    allow_next("prometheus")
    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "development_fixture")
    assert post(client, headers, "prometheus").json()["reason_code"] == "config_disabled"


@pytest.mark.parametrize("reason", ["config_missing", "config_invalid"])
def test_cold_start_keeps_receipts_available_with_broken_prometheus(
    configured, monkeypatch, reason
):
    client, _, prom = configured
    original = post(client, sign_in(client), "database").json()
    if reason == "config_missing":
        prom.unlink()
    else:
        prom.write_text("invalid")
    route(
        monkeypatch, lambda request: pytest.fail("Broken configuration must make zero source calls")
    )

    with TestClient(create_app()) as started:
        assert started.get("/api/health/ready").status_code == 200
        viewer = sign_in(started, "viewer")
        assert started.get(CHECKS).json() == [original]
        assert status(started, "prometheus")["reason_code"] == reason
        collector = next(
            row
            for row in started.get("/api/health").json()["observations"]
            if row["component"] == "collector"
        )
        assert collector["status"] == "unavailable"
        assert post(started, viewer, "prometheus").status_code == 403
        operator = sign_in(started)
        assert post(started, operator, "prometheus").json()["reason_code"] == reason

    monkeypatch.setattr(worker, "run_once", lambda: pytest.fail("Worker must refuse to start"))
    with pytest.raises(ValueError, match="Prometheus read configuration is invalid or unavailable"):
        worker.main()


def test_api_cold_start_still_rejects_invalid_collection_modes(monkeypatch):
    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "prometheus,unknown")
    with pytest.raises(ValueError, match="Unknown collection source mode"):
        with TestClient(create_app()):
            pytest.fail("An invalid provider selection must fail startup")


def test_v2_check_uses_the_exact_configured_target_and_refuses_unapplied_scope(
    configured, monkeypatch, tmp_path
):
    client, kube, _ = configured
    fingerprint = cluster_fingerprint(json.loads(kube.read_text()))
    scope_file = tmp_path / "scope.json"
    raw = json.dumps(
        {
            "schema_version": 2,
            "cluster": {
                "key": "owned",
                "fingerprint_algorithm": "server-ca-sha256/v1",
                "expected_fingerprint": fingerprint,
            },
            "deployments": [{"namespace": "demo", "name": "checkout"}],
        }
    ).encode()
    scope_file.write_bytes(raw)
    monkeypatch.setenv("WORKLOADS_FILE", str(scope_file))
    settings.cache_clear()
    with sessions()() as db, db.begin():
        workload = db.scalar(select(Workload).where(Workload.uid == "uid-1"))
        scope = db.get(ScopeState, 1)
        scope.mode, scope.cluster_key, scope.fingerprint = "v2", "owned", fingerprint
        scope.config_digest = hashlib.sha256(raw).hexdigest()
        db.add(
            ScopeTarget(
                cluster="owned",
                namespace="demo",
                name="checkout",
                state="current",
                workload_id=workload.id,
                last_attempt_at=database_now(db),
            )
        )
    auth = sign_in(client)
    route(monkeypatch, deployment)
    assert post(client, auth, "cluster").json()["reason_code"] == "deployment_read"
    scope_file.write_bytes(raw + b"\n")
    allow_next("cluster")
    assert post(client, auth, "cluster").json()["reason_code"] == "scope_changed"
    assert status(client, "cluster")["status"] == "unconfigured"


def test_wrong_provider_uid_and_changed_config_during_read_are_fenced(configured, monkeypatch):
    client, kube, _ = configured
    headers = sign_in(client)
    responses = iter(("uid-2", "uid-1"))

    def serve(request):
        uid = next(responses)
        if uid == "uid-1":
            config = json.loads(kube.read_text())
            config["token"] = "rotated"
            kube.write_text(json.dumps(config))
        return deployment(request, uid=uid)

    route(monkeypatch, serve)
    assert post(client, headers, "cluster").json()["reason_code"] == "target_changed"
    allow_next("cluster")
    row = post(client, headers, "cluster").json()
    assert row["reason_code"] == "config_changed" and row["historical"]
    assert status(client, "cluster")["status"] == "never_observed"


def test_timeout_replay_and_response_bounds(configured, monkeypatch):
    client, _, _ = configured
    headers = sign_in(client)
    calls = []

    async def serve(request):
        calls.append(request)
        if request.url.host == "kube.test":
            await asyncio.sleep(4)
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * (2 * 1024 * 1024 + 1)))

    route(monkeypatch, serve)
    monkeypatch.setattr(service, "DEADLINE_SECONDS", 0.4)
    key = uuid4()
    started = monotonic()
    timeout = post(client, headers, "cluster", key).json()
    assert monotonic() - started < 3
    assert timeout["reason_code"] == "timeout"
    assert post(client, headers, "cluster", key).json() == timeout
    monkeypatch.setattr(service, "DEADLINE_SECONDS", 8)
    assert post(client, headers, "prometheus").json()["reason_code"] == "response_limit"
    assert len(calls) == 2


@pytest.mark.parametrize("component", ["cluster", "prometheus"])
@pytest.mark.parametrize(
    ("delay", "expected_reason", "expected_requests"),
    [(8.5, "timeout", 0), (3.0, "permission_denied", 1)],
)
def test_initial_config_consumes_deadline_without_holding_admission(
    configured, monkeypatch, component, delay, expected_reason, expected_requests
):
    client, _, _ = configured
    auth = sign_in(client)
    other = TestClient(app)
    other_auth = sign_in(other, "operator")
    prepare = sources.provider_config
    entered, release = Event(), Event()
    requests = []
    elapsed = 0.0

    def slow_config(selected_component):
        nonlocal elapsed
        if selected_component == component and not entered.is_set():
            entered.set()
            assert release.wait(5)
            elapsed = delay
        return prepare(selected_component)

    def serve(request):
        requests.append(request)
        return httpx.Response(403)

    monkeypatch.setattr(sources, "provider_config", slow_config)
    monkeypatch.setattr(service, "monotonic", lambda: monotonic() + elapsed)
    route(monkeypatch, serve)
    key = uuid4()
    with ThreadPoolExecutor(max_workers=1) as executor, ThreadPoolExecutor(max_workers=2) as probe:
        attempt = executor.submit(post, client, auth, component, key)
        try:
            assert entered.wait(5)
            for runtime in ("database", "worker"):
                admitted = probe.submit(post, other, other_auth, runtime).result(timeout=5)
                assert admitted.status_code == 201, admitted.text
        finally:
            release.set()
        result = attempt.result(timeout=6)
    assert result.status_code == 201, result.text
    receipt = result.json()
    assert receipt["reason_code"] == expected_reason
    if expected_reason == "timeout":
        assert receipt["finished_at"] == receipt["attempted_at"]
    assert post(client, auth, component, key).json() == receipt
    assert len(requests) == expected_requests


def test_status_uses_one_snapshot_across_a_concurrent_denial(configured, monkeypatch):
    client, _, _ = configured
    auth = sign_in(client)
    other = TestClient(app)
    other_auth = sign_in(other, "operator")
    deny = False

    def serve(request):
        return httpx.Response(403) if deny else deployment(request)

    route(monkeypatch, serve)
    success = post(client, auth, "cluster").json()
    assert success["reason_code"] == "deployment_read"
    allow_next("cluster")
    deny = True
    assess = service.status_for
    denied = None

    def assess_with_concurrent_commit(db, component, selection, now):
        nonlocal denied
        if component == "cluster" and denied is None:
            response = post(other, other_auth, "cluster")
            assert response.status_code == 201, response.text
            denied = response.json()
        return assess(db, component, selection, now)

    monkeypatch.setattr(service, "status_for", assess_with_concurrent_commit)
    current = status(client, "cluster")
    assert current["latest_check_id"] == success["id"]
    assert current["status"] == "available"
    assert current["last_failure_at"] is None
    current = status(client, "cluster")
    assert denied is not None and current["latest_check_id"] == denied["id"]
    assert current["status"] == "denied"
    assert current["last_success_at"] == success["evidence_at"]
    assert current["last_failure_at"] == denied["finished_at"]

    with sessions()() as db, db.begin():
        db.execute(
            update(ConnectionCheck)
            .where(ConnectionCheck.id == UUID(denied["id"]))
            .values(finished_at=database_now(db) + timedelta(minutes=1))
        )
    assert status(client, "cluster")["reason_code"] == "invalid_timestamp"


@pytest.mark.parametrize("component", ["cluster", "prometheus"])
@pytest.mark.parametrize(("tls_elapsed", "expected_requests"), [(8.5, 0), (7.0, 1)])
def test_provider_deadline_includes_tls_setup(
    configured, monkeypatch, component, tls_elapsed, expected_requests
):
    client, _, _ = configured
    auth = sign_in(client)
    read = sources.read
    kube_context = observation_reader.ssl.create_default_context
    prom_context = PrometheusConfig.ssl_context
    elapsed = 0.0
    tls_calls = []
    requests = []
    cancelled = Event()

    def clock():
        return monotonic() + elapsed

    for module in (service, sources, observation_reader, prometheus):
        monkeypatch.setattr(module, "monotonic", clock)

    def prepare_tls(factory, *args, **kwargs):
        nonlocal elapsed
        context = factory(*args, **kwargs)
        tls_calls.append(True)
        elapsed += tls_elapsed
        return context

    async def serve(request):
        requests.append(request)
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(403)

    async def read_with_delayed_tls(selection, started_at, deadline_at):
        # Apply the delay only to transport setup after admission has committed.
        with monkeypatch.context() as tls:
            if component == "cluster":
                tls.setattr(
                    observation_reader.ssl,
                    "create_default_context",
                    lambda **kw: prepare_tls(kube_context, **kw),
                )
            else:
                tls.setattr(
                    PrometheusConfig, "ssl_context", lambda self: prepare_tls(prom_context, self)
                )
            return await read(
                selection, started_at, deadline_at, transport=httpx.MockTransport(serve)
            )

    monkeypatch.setattr(sources, "read", read_with_delayed_tls)
    key = uuid4()
    response = post(client, auth, component, key)
    assert response.status_code == 201, response.text
    assert response.json()["reason_code"] == "timeout"
    assert post(client, auth, component, key).json() == response.json()
    assert len(tls_calls) == 1
    assert len(requests) == expected_requests
    assert cancelled.is_set() == bool(expected_requests)


def test_authority_guards_and_read_cap(configured, monkeypatch):
    client, _, _ = configured
    route(
        monkeypatch,
        lambda request: pytest.fail("Denied requests and reads must make zero source calls"),
    )
    auth = sign_in(client)
    viewer = TestClient(app)
    viewer_auth = sign_in(viewer, "viewer")
    response = post(viewer, viewer_auth, "cluster")
    assert response.status_code == 403
    assert post(client, {**auth, "Origin": "https://other.test"}, "cluster").status_code == 403
    assert post(client, {**auth, "X-CSRF-Token": "incorrect"}, "cluster").status_code == 403
    assert post(client, auth, "unknown").status_code == 422
    assert (
        client.post(
            CHECKS, json={"component": "cluster", "url": "https://other.test"}, headers=auth
        ).status_code
        == 422
    )
    with sessions()() as db, db.begin():
        assert db.scalar(select(func.count()).select_from(ConnectionCheck)) == 0
    first = post(client, auth, "database").json()
    with sessions()() as db, db.begin():
        actor = UUID(first["created_by"])
        now = database_now(db)
        db.add_all(
            ConnectionCheck(
                component="cluster",
                status="available",
                reason="Legacy receipt",
                source="live_kubernetes",
                created_by=actor,
                observed_at=now - timedelta(seconds=i + 1),
            )
            for i in range(54)
        )
    receipts = viewer.get(CHECKS).json()
    assert len(receipts) == 50 and receipts[0]["id"] == first["id"]
    assert all(row["historical"] for row in receipts[1:])
    assert status(viewer, "cluster")["status"] == "never_observed"
    with sessions()() as db, db.begin():
        db.execute(
            update(LoginSession)
            .where(LoginSession.user_id == actor)
            .values(expires_at=database_now(db) - timedelta(seconds=1))
        )
    assert post(client, auth, "cluster").status_code == 401
    assert client.get(f"{CHECKS}/status").status_code == 401
    with sessions()() as db, db.begin():
        assert db.scalar(select(func.count()).select_from(ConnectionCheck)) == 55


def test_abandoned_attempt_expires_without_network_or_timestamp_refresh(configured, monkeypatch):
    client, _, _ = configured
    auth = sign_in(client)
    original = post(client, auth, "database").json()
    key = uuid4()
    with sessions()() as db, db.begin():
        selection = sources.selected(db, "cluster", sources.prepare_config("cluster"))
        deadline = database_now(db) - timedelta(seconds=1)
        row = ConnectionCheck(
            component="cluster",
            source="live_kubernetes",
            status="unavailable",
            reason="A connection check is in progress.",
            reason_code="check_pending",
            attempted_at=deadline - timedelta(seconds=8),
            deadline_at=deadline,
            observed_at=deadline - timedelta(seconds=8),
            created_by=UUID(original["created_by"]),
            idempotency_key=key,
            binding_key=selection.key,
        )
        db.add(row)
        db.flush()
        attempt_id = row.id
    route(
        monkeypatch,
        lambda request: pytest.fail(
            "Reading or replaying expired attempts must make zero source calls"
        ),
    )
    assert status(client, "cluster")["reason_code"] == "timeout"
    history = client.get(CHECKS).json()
    assert [item["id"] for item in history] == [original["id"], str(attempt_id)]
    assert history[0]["observed_at"] >= history[1]["observed_at"]
    assert history[1]["reason_code"] == "timeout"
    with sessions()() as db:
        assert db.get(ConnectionCheck, attempt_id).finished_at is None
    with TestClient(app) as restarted:
        new_auth = sign_in(restarted)
        timed_out = post(restarted, new_auth, "cluster", key).json()
        assert timed_out["id"] == str(attempt_id) and timed_out["reason_code"] == "timeout"
        assert timed_out == history[1]
        assert post(restarted, new_auth, "cluster", key).json() == timed_out
        assert status(restarted, "cluster")["last_failure_at"] == timed_out["finished_at"]


def test_migration_retains_legacy_receipt_bytes(seeded):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0017_recommendation_reports")
    try:
        with sessions()() as db, db.begin():
            receipt_id = uuid4()
            actor = db.scalar(text("SELECT id FROM users WHERE username = 'operator'"))
            created = db.execute(
                text("""INSERT INTO connection_checks
                (id, component, status, reason, source, created_by)
                VALUES (:id, 'cluster', 'available', 'Saved receipt', 'live_kubernetes', :actor)
                RETURNING observed_at"""),
                {"id": receipt_id, "actor": actor},
            ).scalar_one()
    finally:
        command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sessions()() as db:
        row = db.get(ConnectionCheck, receipt_id)
        assert row.reason == "Saved receipt" and row.observed_at == created
        assert row.attempted_at is None and row.binding_key is None
