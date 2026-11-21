"""PostgreSQL health projection acceptance using controlled provider inputs."""

import asyncio
import base64
import json
import socket
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, func, select, update

from conftest import sign_in
from observation_fixtures import observed
from test_recommendation_reports import controlled_result

from cloudops.entrypoints import worker
from cloudops.features.access.models import LoginSession, User
from cloudops.features.health import api, service
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.schemas import (
    QualifiedEvidence,
    ReportResult,
    ToolInvocation,
)
from cloudops.features.workloads import observation_service, reconcile
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_models import ObservationAttempt, ObservationState
from cloudops.features.workloads.scope_config import Lookup
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, engine, sessions
from cloudops.shared.jobs import Job


def read_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    value = response.json()
    return {item["component"]: item for item in value["observations"]}, value["collection_dispatch"]


def test_worker_threshold_and_public_probes(client, monkeypatch):
    sign_in(client, "viewer")
    with sessions()() as db:
        initial = database_now(db)
    clock = [initial]
    monkeypatch.setattr(service, "database_now", lambda db: clock[0])
    monkeypatch.setattr(worker, "database_now", lambda db: clock[0])
    monkeypatch.setattr(service, "collection_dispatch_status", lambda: (False, "class_denied"))

    rows, dispatch = read_health(client)
    assert rows["worker"]["status"] == "never_observed"
    assert rows["worker"]["observed_at"] is None
    assert dispatch == {"status": "paused", "reason_code": "class_denied"}

    worker.heartbeat()
    for age, status in [(0, "fresh"), (44.999999, "fresh"), (45, "fresh"), (45.000001, "stale")]:
        clock[0] = initial + timedelta(seconds=age)
        rows, dispatch = read_health(client)
        assert rows["worker"]["status"] == status
        assert datetime.fromisoformat(rows["worker"]["observed_at"]) == initial
        assert datetime.fromisoformat(rows["worker"]["assessed_at"]) == clock[0]
        assert rows["database"]["status"] == "available"
        assert dispatch["status"] == "paused"

    worker.heartbeat()
    assert read_health(client)[0]["worker"]["status"] == "fresh"
    clock[0] -= timedelta(seconds=2)
    assert read_health(client)[0]["worker"]["reason_code"] == "invalid_evidence_time"

    client.cookies.clear()
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health/live").status_code == 200
    assert client.get("/api/health/ready").status_code == 200

    def unavailable():
        raise RuntimeError("controlled readiness failure")

    monkeypatch.setattr(api, "require_schema", unavailable)
    assert client.get("/api/health/ready").status_code == 503
    assert client.get("/api/health/live").status_code == 200


@pytest.mark.parametrize("mode", ["legacy", "v2"])
def test_current_cluster_observation_requires_exact_uid_attempt_and_config(
    client, tmp_path, monkeypatch, mode
):
    sign_in(client)
    fixture = read_health(client)[0]["kubernetes"]
    assert fixture["source"] == "development_fixture"
    assert fixture["status"] != "fresh"

    scope_path, reader_path = tmp_path / "scope.json", tmp_path / "reader.json"
    monkeypatch.setenv("WORKLOADS_FILE", str(scope_path))
    monkeypatch.setenv("KUBE_READ_CONFIG", str(reader_path))
    settings.cache_clear()
    uid = ["deploy-uid"]
    result = ["complete"]

    def write_reader(server, token="private-reader-sentinel"):
        reader_path.write_text(
            json.dumps(
                {
                    "server": server,
                    "ca_data": base64.b64encode(b"controlled-ca").decode(),
                    "token": token,
                }
            ),
            encoding="utf-8",
        )

    class ScopeReader:
        def __init__(self, *args):
            pass

        def lookup(self, target, deadline):
            return Lookup("present", uid[0])

    class ObservationReader:
        def __init__(self, **kwargs):
            pass

        async def read(self, identity, started_at):
            if result[0] == "denied":
                raise ObservationError("permission_denied")
            value = observed(identity, started_at)
            if result[0] == "partial":
                return value.model_copy(
                    update={"coverage": "partial", "reasons": ["read_window_changed"]}
                )
            return value

    monkeypatch.setattr(reconcile, "ScopeReader", ScopeReader)
    monkeypatch.setattr(observation_service, "ObservationReader", ObservationReader)

    def sync():
        value = (
            [
                {
                    "cluster": "lab",
                    "namespace": "demo",
                    "name": "checkout",
                    "uid": uid[0],
                    "source": "live_kubernetes",
                }
            ]
            if mode == "legacy"
            else {
                "schema_version": 2,
                "cluster": {
                    "key": "lab",
                    "fingerprint_algorithm": "server-ca-sha256/v1",
                    "expected_fingerprint": "a" * 64,
                },
                "deployments": [{"namespace": "demo", "name": "checkout"}],
            }
        )
        scope_path.write_text(json.dumps(value), encoding="utf-8")
        with sessions()() as db:
            reconcile.sync(db)
            return db.scalar(select(Workload.id).where(Workload.active, Workload.uid == uid[0]))

    try:
        write_reader("https://cluster-a.invalid")
        workload_id = sync()

        def get_cluster():
            return read_health(client)[0]["kubernetes"]

        assert get_cluster()["status"] == "never_observed"
        complete = asyncio.run(observation_service.observe_direct(workload_id)).observation
        last_success = get_cluster()
        assert last_success["status"] == "fresh"
        assert last_success["target"]["uid"] == uid[0]
        assert datetime.fromisoformat(last_success["observed_at"]) == complete.observed_at
        assert last_success["config_id"] == last_success["last_success_config_id"]

        with monkeypatch.context() as frozen:
            for age, expected in [
                (59.999999, "fresh"),
                (60, "fresh"),
                (60.000001, "stale"),
                (-2, "unknown"),
            ]:
                stamp = complete.observed_at + timedelta(seconds=age)
                frozen.setattr(service, "database_now", lambda db: stamp)
                assert get_cluster()["status"] == expected

        write_reader("https://cluster-a.invalid", token="rotated-reader")
        rotated = get_cluster()
        assert rotated["status"] == "unknown" and rotated["observed_at"] is None
        assert rotated["config_id"] == last_success["config_id"]
        assert rotated["last_success_at"] == last_success["last_success_at"]
        write_reader("https://cluster-a.invalid")

        for outcome in ("denied", "partial"):
            result[0] = outcome
            direct = asyncio.run(observation_service.observe_direct(workload_id))
            row = get_cluster()
            assert row["status"] == outcome
            assert row["last_success_at"] == last_success["last_success_at"]
            assert row["last_attempt_at"] is not None and row["last_failure_at"] is not None
            if outcome == "partial":
                assert datetime.fromisoformat(row["observed_at"]) == direct.observation.observed_at
                with monkeypatch.context() as frozen:
                    from cloudops.features.workloads.observation_settings import POLICY

                    expired = datetime.fromisoformat(row["last_failure_at"]) + timedelta(
                        days=POLICY.history_days, microseconds=1
                    )
                    frozen.setattr(service, "database_now", lambda db: expired)
                    retained = get_cluster()
                    assert retained["reason_code"] == "observation_expired"
                    assert retained["observed_at"] is None
            else:
                assert row["observed_at"] is None

        private, public = observation_service.reader_bindings()
        pending = observation_service.claim(
            workload_id, "baseline_candidate", reader_identity=private, public_reader=public
        )
        assert get_cluster()["status"] != "fresh"
        observation_service.finalize(pending, observed(pending.identity, pending.started_at))
        assert get_cluster()["status"] == "fresh"
        write_reader("https://cluster-b.invalid")
        prior = get_cluster()
        assert prior["status"] == "unknown"
        assert prior["last_success_at"] is not None
        assert prior["config_id"] != prior["last_success_config_id"]
        write_reader("https://cluster-a.invalid")
        invalid = json.loads(reader_path.read_text(encoding="utf-8"))
        del invalid["token"]
        reader_path.write_text(json.dumps(invalid), encoding="utf-8")
        assert get_cluster()["reason_code"] == "reader_unconfigured"
        write_reader("https://cluster-a.invalid")
        with sessions()() as db, db.begin():
            previous = db.get(ObservationState, workload_id)
            # An older worker advances the attempt while leaving the new binding columns intact.
            previous.sequence += 1
            previous.success_sequence = previous.sequence
            previous.last_attempt_at = database_now(db)
        unbound = get_cluster()
        assert unbound["status"] == "unknown"
        assert unbound["last_success_at"] == prior["last_success_at"]
        scope_path.write_text(scope_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        assert get_cluster()["reason_code"] == "scope_configuration_changed"
        assert sync() == workload_id
        assert get_cluster()["status"] == "unknown"

        uid[0] = "new-uid-same-name"
        assert sync() != workload_id
        row = get_cluster()
        assert row["target"]["uid"] == uid[0]
        assert row["status"] == "never_observed"
        assert row["last_success_at"] is None
    finally:
        settings.cache_clear()


def test_poll_is_bounded_read_only_and_retains_krr_evidence(client, db, monkeypatch):
    sign_in(client, "viewer")
    monkeypatch.setenv("EXECUTOR_URL", "http://private-gateway.invalid")
    monkeypatch.setenv("KRR_RUNNER_URL", "http://private-runner.invalid")
    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "live_kubernetes,prometheus")

    def outbound(*args, **kwargs):
        pytest.fail("A passive GET attempted an outbound provider call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", outbound)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", outbound)
    monkeypatch.setattr(socket, "create_connection", outbound)

    def poll():
        statements = []

        def capture(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine(), "before_cursor_execute", capture)
        try:
            rows, dispatch = read_health(client)
        finally:
            event.remove(engine(), "before_cursor_execute", capture)
        assert 1 <= len(statements) <= 8
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert sum("LIMIT" in statement for statement in statements) >= 2
        return rows, len(statements)

    rows, query_count = poll()
    assert rows["krr_runner"]["status"] == "unknown"
    assert rows["krr_report"]["status"] == "never_observed"
    assert rows["collector"]["configured_adapters"] == ["live_kubernetes", "prometheus"]
    assert rows["collector"]["last_success_at"] is None
    assert rows["executor"]["status"] == "see_feature"

    with db.begin():
        owner = db.scalar(select(User).where(User.username == "operator"))
        workload = db.scalar(select(Workload))
        finished = database_now(db) - timedelta(hours=3)
        row = RecommendationReport(
            workload_id=workload.id,
            created_by=owner.id,
            created_at=finished - timedelta(minutes=2),
            finished_at=finished,
            schema_version=2,
            state="available",
            input_identity={
                "workload_id": str(workload.id),
                "uid": workload.uid,
                "cluster": workload.cluster,
                "name": workload.name,
                "namespace": workload.namespace,
            },
            profile={"history_hours": 6},
        )
        collected_at = finished - timedelta(seconds=20)
        result = controlled_result(row, collected_at)
        result.source_evidence = QualifiedEvidence(
            identity=result.identity,
            window=result.window,
            query_digest=result.query_digest,
            config_digest="1" * 64,
            history_digest="2" * 64,
            collected_at=collected_at,
            pods=[],
            resources=[],
        )
        result.tool_invocation = ToolInvocation(
            request_sha256="3" * 64,
            started_at=finished - timedelta(seconds=10),
        )
        row.result_v2 = ReportResult.model_validate(result.model_dump()).model_dump(mode="json")
        db.add(row)
        db.add_all(
            [
                RecommendationReport(
                    workload_id=workload.id, created_by=owner.id, state="unavailable"
                )
                for _ in range(250)
            ]
        )
        db.add_all(
            [
                Workload(
                    cluster="fixture",
                    namespace="demo",
                    name=f"retired-{index}",
                    uid=f"retired-{index}",
                    active=False,
                )
                for index in range(250)
            ]
        )
        db.flush()
        report_id = row.id

    for _ in range(3):
        rows, count = poll()
        assert count == query_count
        assert rows["krr_runner"]["status"] == "unknown"
        historical = rows["krr_report"]
        assert historical["status"] == "historical"
        assert historical["report_id"] == str(report_id)
        assert historical["target"]["uid"] == workload.uid
        assert historical["config_id"] == "1" * 64
        assert datetime.fromisoformat(historical["observed_at"]) == collected_at
        assert datetime.fromisoformat(historical["last_success_at"]) == finished
        assert "private-" not in json.dumps(rows)

    monkeypatch.delenv("KRR_RUNNER_URL")
    rows, _ = poll()
    assert rows["krr_runner"]["status"] == "unconfigured"
    assert rows["krr_report"]["report_id"] == str(report_id)
    with db.begin():
        row = db.get(RecommendationReport, report_id)
        row.result_v2 = {"state": "unavailable"}
    rows, _ = poll()
    assert rows["krr_report"]["reason_code"] == "report_invalid"
    assert rows["krr_report"]["last_success_at"] is None
    with db.begin():
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(ObservationAttempt)) == 0
        db.execute(update(LoginSession).values(expires_at=database_now(db) - timedelta(seconds=1)))
    assert client.get("/api/health").status_code == 401
