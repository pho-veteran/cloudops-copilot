"""Issue #36: authenticated Alertmanager episodes, UID-bound admission and intake receipts."""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from conftest import sign_in
from fastapi.testclient import TestClient
from intake_fixtures import (
    ANNOTATION,
    READER_SECRET,
    SOURCE,
    TOKEN,
    Cluster,
    alert,
    configure_intake,
    configure_reader,
    deliver,
    envelope,
    pin_scope,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from test_incident_requests import capacity as _capacity, free_capacity

from cloudops.entrypoints.api import create_app
from cloudops.entrypoints.dispatch import GATE
from cloudops.features.incidents import intake_service
from cloudops.features.incidents.intake_models import AlertEpisode, AlertReceipt
from cloudops.features.incidents.intake_schemas import canonical_start
from cloudops.features.incidents.intake_settings import MAX_BODY_BYTES, intake_config
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.promotion import promote_deferred
from cloudops.features.workloads import alert_identity
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import BACKEND_ROOT, database_now, sessions
from cloudops.shared.jobs import Job

capacity = _capacity
STARTS = "2026-11-01T10:00:00.123456789Z"


def count(model, *where):
    with sessions()() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def rows(model, *where):
    with sessions()() as db:
        return db.scalars(select(model).where(*where)).all()


def episode(fingerprint, starts=STARTS):
    with sessions()() as db:
        return db.scalar(
            select(AlertEpisode).where(
                AlertEpisode.fingerprint == fingerprint,
                AlertEpisode.starts_at_key == canonical_start(starts)[0],
            )
        )


def domain_counts():
    return tuple(count(m) for m in (Incident, InvestigationRun, Job, AlertEpisode, AlertReceipt))


@pytest.fixture
def intake(client, tmp_path, monkeypatch):
    """Configured sender, pinned v2 scope (checkout, payments) and a controlled cluster."""
    configure_intake(tmp_path, monkeypatch)
    fingerprint = configure_reader(tmp_path, monkeypatch)
    ids = pin_scope(fingerprint, {"checkout": "uid-checkout", "payments": "uid-payments"})
    cluster = Cluster()
    cluster.deployment("checkout", "uid-checkout")
    monkeypatch.setattr(intake_service, "reader_transport", cluster.transport)
    cluster.ids = ids
    cluster.fingerprint = fingerprint
    cluster.tmp_path = tmp_path
    return cluster


def test_credentials_and_envelopes_are_checked_before_any_write(client, intake, monkeypatch):
    before = domain_counts()
    body = envelope()
    monkeypatch.delenv("ALERT_INTAKE_CONFIG")
    intake_config.cache_clear()
    response = deliver(client, body)
    assert response.status_code == 503 and response.json()["error"]["code"] == "intake_unavailable"
    configure_intake(intake.tmp_path, monkeypatch)

    def unauthorized(response):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "intake_unauthorized"
        assert TOKEN not in response.text

    unauthorized(deliver(client, body, token=None))
    unauthorized(deliver(client, body, token="wrong-token"))
    unauthorized(deliver(client, body, token=None, headers={"Authorization": f"Basic {TOKEN}"}))
    unauthorized(deliver(client, body, token=None, headers={"Authorization": TOKEN}))
    unauthorized(
        client.post(
            "/api/intake/alertmanager",
            content=json.dumps(body),
            headers=[
                ("Content-Type", "application/json"),
                ("Authorization", f"Bearer {TOKEN}"),
                ("Authorization", f"Bearer {TOKEN}"),
            ],
        )
    )
    # A browser cookie, even carrying the sender secret, is not machine authentication.
    client.cookies.set("cloudops_session", TOKEN)
    unauthorized(deliver(client, body, token=None))
    assert client.get("/api/incidents").json()["error"]["code"] == "session_expired"
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/incidents", headers=bearer).status_code == 401

    def refused(response, status, code):
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code

    refused(deliver(client, body, headers={"Content-Type": "text/plain"}), 415, "intake_media_type")
    refused(deliver(client, b"x" * (MAX_BODY_BYTES + 1)), 413, "intake_too_large")

    def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b" " * 65536

    streamed = client.post(
        "/api/intake/alertmanager",
        content=chunks(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    refused(streamed, 413, "intake_too_large")
    invalid = [
        b"{not json",
        {**body, "version": "3"},
        {**body, "source_id": "spoofed"},
        {**body, "origin": {"kind": "operator"}},
        envelope({**alert(), "source_id": "spoofed"}),
        envelope({**alert(), "origin": {"kind": "operator"}}),
        {**body, "alerts": []},
        {**body, "alerts": [alert(f"{i:x}") for i in range(101)]},
        envelope(alert(namespace="x" * 1025)),
        envelope(alert(starts="2026-11-01 10:00:00")),
        envelope(alert(starts="9999-12-31T23:59:59-23:00")),
        envelope(alert(starts="0001-01-01T00:00:00+23:00")),
        envelope(alert(starts="0001-01-01T00:00:00Z")),
        envelope(alert(fingerprint="NOT-HEX")),
    ]
    for value in invalid:
        refused(deliver(client, value), 422, "intake_invalid")
    assert domain_counts() == before
    assert intake.calls == []


def test_direct_and_pod_alerts_admit_one_uid_bound_incident_each(client, intake):
    response = deliver(client, envelope(alert("a1"), receiver="forward-compatible-receiver"))
    assert response.status_code == 200, response.text
    assert response.json()["counts"] == {"accepted": 1}
    assert ANNOTATION not in response.text
    bound = episode("a1")
    assert (bound.outcome, bound.workload_uid, bound.signal) == (
        "accepted",
        "uid-checkout",
        "firing",
    )
    assert str(bound.workload_id) == intake.ids["checkout"]
    with sessions()() as db:
        incident = db.get(Incident, bound.incident_id)
        run = db.get(InvestigationRun, bound.run_id)
        assert (incident.origin_kind, incident.origin_source_id) == ("alertmanager", SOURCE)
        assert incident.created_by is None and incident.state == "open"
        assert incident.title == "CheckoutDown · demo/checkout"
        assert ANNOTATION not in incident.description
        assert incident.first_seen_at == datetime(2026, 11, 1, 10, 0, 0, 123456, timezone.utc)
        assert incident.last_seen_at is not None and incident.alert_signal == "firing"
        assert (incident.severity, incident.severity_provenance) == ("critical", "alert_label")
        assert (run.state, run.queue_reason, run.question_source) == ("queued", None, "automatic")
        assert run.window_source == "automatic_default" and run.requested_by is None
        assert run.question == "Why is CheckoutDown firing for demo/checkout?"
        assert db.scalar(select(func.count()).select_from(Job).where(Job.subject_id == run.id)) == 1
    [deployment_read] = intake.calls
    assert deployment_read.url.path == "/apis/apps/v1/namespaces/demo/deployments/checkout"

    intake.pod("checkout-7c9-x2", "checkout", "uid-checkout")
    response = deliver(client, envelope(alert("b2", deployment=None, pod="checkout-7c9-x2")))
    assert response.json()["counts"] == {"accepted": 1}
    assert episode("b2").workload_uid == "uid-checkout"
    assert [c.url.path.rsplit("/", 2)[1] for c in intake.calls[1:]] == ["pods", "replicasets"]
    assert count(Incident) == 2
    receipts = rows(AlertReceipt)
    assert {r.outcome for r in receipts} == {"accepted"}
    first = next(r for r in receipts if r.fingerprint == "a1")
    assert (first.incident_id, first.run_id) == (bound.incident_id, bound.run_id)
    assert (first.alertname, first.namespace, first.target_kind) == (
        "CheckoutDown",
        "demo",
        "deployment",
    )


@pytest.mark.parametrize(
    ("labels", "reason", "network"),
    [
        ({"deployment": "unconfigured"}, "out_of_scope", False),
        ({"namespace": "elsewhere"}, "out_of_scope", False),
        ({"cluster": "other-cluster"}, "out_of_scope", False),
        ({"deployment": None}, "identity_missing", False),
        ({"deployment": "Bad_Name"}, "identity_invalid", False),
        ({"deployment": "payments"}, "identity_missing", True),
        ({"deployment": "checkout", "uid": "changed"}, "identity_changed", True),
        (
            {"deployment": None, "pod": "stateful-0", "kind": "statefulset"},
            "unsupported_identity",
            True,
        ),
        ({"deployment": None, "pod": "two-0", "kind": "two"}, "identity_ambiguous", True),
        ({"deployment": "payments", "pod": "checkout-1"}, "identity_ambiguous", True),
    ],
)
def test_permanent_identity_refusals_leave_a_terminal_marker(
    client, intake, labels, reason, network
):
    labels = dict(labels)
    kind, uid = labels.pop("kind", None), labels.pop("uid", None)
    if uid:
        intake.deployment("checkout", uid)
    if kind == "statefulset":
        intake.pods["stateful-0"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "name": "s",
                "uid": "s",
                "controller": True,
            }
        ]
    if kind == "two":
        owner = {"apiVersion": "apps/v1", "kind": "ReplicaSet", "uid": "r", "controller": True}
        intake.pods["two-0"] = [{**owner, "name": "a"}, {**owner, "name": "b"}]
    if labels.get("pod") == "checkout-1":
        intake.pod("checkout-1", "checkout", "uid-checkout")
    body = envelope(alert("c3", **labels))
    response = deliver(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["counts"] == {"rejected": 1}
    marker = episode("c3")
    assert (marker.outcome, marker.reason, marker.incident_id) == ("rejected", reason, None)
    assert bool(intake.calls) == network
    assert count(Incident) == 0
    calls = len(intake.calls)
    again = deliver(client, body)
    assert again.json()["counts"] == {"duplicate": 1}
    assert len(intake.calls) == calls, "A known episode is never remapped"
    assert rows(AlertReceipt, AlertReceipt.outcome == "duplicate")[0].reason == reason


@pytest.mark.parametrize("failure", ["forbidden", "redirect", "server", "fingerprint", "unpinned"])
def test_transient_reads_are_retryable_without_a_marker(client, intake, monkeypatch, failure):
    path = "/apis/apps/v1/namespaces/demo/deployments/checkout"
    if failure == "forbidden":
        intake.status[path] = 403
    if failure == "server":
        intake.status[path] = 500
    if failure == "redirect":
        original = intake.handler

        def redirect(request):
            if request.url.host == "cluster.example":
                intake.calls.append(request)
                return httpx.Response(302, headers={"Location": "http://attacker.invalid/steal"})
            return original(request)

        monkeypatch.setattr(intake_service, "reader_transport", httpx.MockTransport(redirect))
    if failure == "fingerprint":
        with sessions()() as db, db.begin():
            db.execute(text("UPDATE workload_scope_state SET fingerprint = :f"), {"f": "0" * 64})
    if failure == "unpinned":
        configure_intake(intake.tmp_path, monkeypatch, cluster="another-cluster")
    response = deliver(client, envelope(alert("d4")))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "intake_retry"
    assert episode("d4") is None and count(Incident) == 0
    [receipt] = rows(AlertReceipt)
    expected = "scope_unpinned" if failure == "unpinned" else "cluster_unavailable"
    assert (receipt.outcome, receipt.reason) == ("retryable", expected)
    if failure == "redirect":
        assert len(intake.calls) == 1, "Redirects are never followed with the bearer token"
    if failure in ("forbidden", "server"):
        intake.status.clear()
        assert deliver(client, envelope(alert("d4"))).json()["counts"] == {"accepted": 1}


@pytest.mark.parametrize("boundary", ["scope_snapshot", "find"])
def test_database_read_failure_is_retryable_without_admission(
    client, intake, monkeypatch, boundary
):
    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(intake_service, boundary, unavailable)
    response = deliver(client, envelope(alert("d6")))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "intake_retry"
    assert episode("d6") is None and count(Incident) == 0
    assert intake.calls == []


def test_slow_stream_is_cancelled_at_absolute_notification_deadline(client, intake, monkeypatch):
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b'{"kind":', b'"Deployment",', b'"metadata":', b"{}}"):
                await asyncio.sleep(0.05)
                yield chunk

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(alert_identity, "DEADLINE_SECONDS", 0.12)
    monkeypatch.setattr(
        intake_service,
        "reader_transport",
        httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream())),
    )
    response = deliver(client, envelope(alert("d5")))
    assert response.status_code == 503
    assert episode("d5") is None and count(Incident) == 0
    assert [(r.outcome, r.reason) for r in rows(AlertReceipt)] == [
        ("retryable", "budget_exhausted")
    ]
    assert closed


def test_concurrent_and_equivalent_deliveries_share_one_episode(client, intake):
    app = create_app()

    def send(_):
        with TestClient(app) as other:
            return deliver(other, envelope(alert("e5"))).json()["counts"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert sorted(results, key=str) == sorted([{"accepted": 1}] + [{"duplicate": 1}] * 3, key=str)
    assert (count(Incident), count(InvestigationRun), count(AlertEpisode)) == (1, 1, 1)
    equivalent = deliver(
        client, envelope(alert("e5", starts="2026-11-01T17:00:00.123456789+07:00"))
    )
    assert equivalent.json()["counts"] == {"duplicate": 1}
    padded = deliver(client, envelope(alert("e5", starts="2026-11-01T10:00:00.123456789000Z")))
    assert padded.status_code == 422, "More than nine fractional digits is not RFC3339Nano"
    nano = deliver(client, envelope(alert("e5", starts="2026-11-01T10:00:00.123456788Z")))
    assert nano.json()["counts"] == {"accepted": 1}, "A different nanosecond is a new episode"
    trimmed = deliver(client, envelope(alert("e6", starts="2026-11-01T10:00:00.5Z")))
    zeros = deliver(client, envelope(alert("e6", starts="2026-11-01T10:00:00.500000000Z")))
    assert (trimmed.json()["counts"], zeros.json()["counts"]) == ({"accepted": 1}, {"duplicate": 1})
    keys = {e.starts_at_key for e in rows(AlertEpisode)}
    assert keys == {
        "2026-11-01T10:00:00.123456789Z",
        "2026-11-01T10:00:00.123456788Z",
        "2026-11-01T10:00:00.500000000Z",
    }


def test_identity_change_between_read_and_commit_never_binds_a_replacement(
    client, intake, monkeypatch
):
    original = alert_identity.IdentityResolver.resolve

    def resolve_then_replace(self, labels):
        result = original(self, labels)
        with sessions()() as db, db.begin():
            db.execute(update(Workload).where(Workload.name == "checkout").values(active=False))
            db.add(
                Workload(
                    cluster="lab",
                    namespace="demo",
                    name="checkout",
                    uid="uid-new",
                    source="live_kubernetes",
                )
            )
        return result

    monkeypatch.setattr(alert_identity.IdentityResolver, "resolve", resolve_then_replace)
    response = deliver(client, envelope(alert("f6")))
    assert response.json()["counts"] == {"rejected": 1}
    assert (episode("f6").reason, count(Incident)) == ("identity_changed", 0)
    monkeypatch.setattr(alert_identity.IdentityResolver, "resolve", original)
    # The delayed old episode stays inert; only a new firing start reaches the replacement.
    assert deliver(client, envelope(alert("f6"))).json()["counts"] == {"duplicate": 1}
    intake.deployment("checkout", "uid-new")
    later = deliver(client, envelope(alert("f6", starts="2026-11-01T11:00:00Z")))
    assert later.json()["counts"] == {"accepted": 1}
    assert episode("f6", "2026-11-01T11:00:00Z").workload_uid == "uid-new"


@pytest.mark.parametrize("change", ["epoch", "publication", "rotation", "retirement", "refusal"])
def test_scope_authority_change_during_mapping_is_retryable(client, intake, monkeypatch, change):
    original = alert_identity.IdentityResolver.resolve

    def resolve_then_publish(self, labels):
        result = original(self, labels)
        with sessions()() as db, db.begin():
            if change in ("epoch", "retirement"):
                values = (
                    {"observation_epoch": Workload.observation_epoch + 1}
                    if change == "epoch"
                    else {"active": False}
                )
                db.execute(update(Workload).where(Workload.name == "checkout").values(**values))
            elif change == "rotation":
                db.execute(
                    text("UPDATE workload_scope_state SET fingerprint = :f"), {"f": "0" * 64}
                )
            else:
                db.execute(
                    text("UPDATE workload_scope_state SET applied_sequence = applied_sequence + 1")
                )
        return result

    monkeypatch.setattr(alert_identity.IdentityResolver, "resolve", resolve_then_publish)
    payload = envelope(alert("f7", deployment="unknown" if change == "refusal" else "checkout"))
    response = deliver(client, payload)
    assert response.status_code == 503
    assert episode("f7") is None and count(Incident) == 0
    assert rows(AlertReceipt)[0].reason == "scope_changed"
    monkeypatch.setattr(alert_identity.IdentityResolver, "resolve", original)
    if change == "rotation":
        with sessions()() as db, db.begin():
            db.execute(
                text("UPDATE workload_scope_state SET fingerprint = :f"),
                {"f": intake.fingerprint},
            )
    if change == "retirement":
        with sessions()() as db, db.begin():
            db.execute(update(Workload).where(Workload.name == "checkout").values(active=True))
    if change != "refusal":
        assert deliver(client, payload).json()["counts"] == {"accepted": 1}


def test_each_alert_status_is_processed_independently(client, intake):
    headers = sign_in(client)
    assert deliver(client, envelope(alert("a1"))).json()["counts"] == {"accepted": 1}
    first = episode("a1")
    mixed = envelope(
        alert("a1", status="resolved"),
        alert("f9", status="resolved"),
        alert("c1", starts="2026-11-01T10:05:00Z"),
        status="firing",
        truncatedAlerts=7,
    )
    response = deliver(client, mixed)
    assert response.json()["counts"] == {"resolved_signal": 1, "ignored": 1, "accepted": 1}
    with sessions()() as db:
        incident = db.get(Incident, first.incident_id)
        assert (incident.alert_signal, incident.state) == ("resolved", "open")
        assert db.get(InvestigationRun, first.run_id).state == "queued"
    unmatched = episode("f9")
    assert (unmatched.outcome, unmatched.reason) == ("ignored", "resolved_before_admission")
    calls = len(intake.calls)
    # A delayed firing for the same resolved episode cannot create work.
    assert deliver(client, envelope(alert("f9"))).json()["counts"] == {"duplicate": 1}
    assert len(intake.calls) == calls and count(Incident) == 2
    # Resolved is terminal for the episode signal; a repeat neither revives nor reopens.
    assert deliver(client, envelope(alert("a1"))).json()["counts"] == {"duplicate": 1}
    assert rows(Incident, Incident.id == first.incident_id)[0].alert_signal == "resolved"
    # A permanent rejection stays terminal after scope later includes the target.
    assert deliver(client, envelope(alert("a7", deployment="ledger"))).json()["counts"] == {
        "rejected": 1
    }
    pin_scope(intake.fingerprint, {"ledger": "uid-ledger"})
    intake.deployment("ledger", "uid-ledger")
    assert deliver(client, envelope(alert("a7", deployment="ledger"))).json()["counts"] == {
        "duplicate": 1
    }
    fresh = envelope(alert("a7", deployment="ledger", starts="2026-11-01T12:00:00Z"))
    assert deliver(client, fresh).json()["counts"] == {"accepted": 1}
    # Lifecycle stays human-owned: resolving does not touch the alert episode binding.
    resolved = client.post(
        f"/api/incidents/{first.incident_id}/lifecycle",
        json={"action": "resolve", "reason": "Handled by an Operator"},
        headers=headers,
    )
    assert resolved.status_code == 200
    assert episode("a1").incident_id == first.incident_id


def test_capacity_defers_refuses_replays_and_promotes_the_same_run(client, intake, capacity):
    headers = sign_in(client)
    capacity(jobs=1, deferred=1, filled=1)
    deferred = deliver(client, envelope(alert("b1")))
    assert deferred.json()["counts"] == {"deferred": 1}
    accepted = episode("b1")
    assert rows(InvestigationRun, InvestigationRun.id == accepted.run_id)[0].queue_reason == (
        "capacity"
    )
    before = (count(Incident), count(InvestigationRun), count(AlertEpisode))
    full = deliver(client, envelope(alert("b3")))
    assert full.status_code == 503 and episode("b3") is None
    assert (count(Incident), count(InvestigationRun), count(AlertEpisode)) == before
    assert rows(AlertReceipt, AlertReceipt.fingerprint == "b3")[0].reason == "capacity_exhausted"
    # Both bounds full: the accepted episode still returns its identities and actual state.
    replay = deliver(client, envelope(alert("b1")))
    assert replay.json()["counts"] == {"duplicate": 1}
    link = rows(AlertReceipt, AlertReceipt.outcome == "duplicate")[0]
    assert (link.incident_id, link.run_id) == (accepted.incident_id, accepted.run_id)
    free_capacity()
    assert promote_deferred(gate=GATE) == 1
    with sessions()() as db:
        run = db.get(InvestigationRun, accepted.run_id)
        assert run.queue_reason is None
        assert db.scalar(select(func.count()).select_from(Job).where(Job.subject_id == run.id)) == 1
    for action in ("resolve", "reopen"):
        response = client.post(
            f"/api/incidents/{accepted.incident_id}/lifecycle",
            json={"action": action, "reason": "Lifecycle does not refund automatic work"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    runs = count(InvestigationRun)
    assert deliver(client, envelope(alert("b1"))).json()["counts"] == {"duplicate": 1}
    assert count(InvestigationRun) == runs


def test_partial_batches_retry_without_duplicates_and_survive_restart(client, intake, capacity):
    capacity(jobs=1, deferred=0, filled=1)
    batch = envelope(
        alert("d1", deployment="unconfigured"),
        alert("d2"),
        alert("d3", status="resolved"),
    )
    response = deliver(client, batch)
    assert response.status_code == 503
    assert {(e.fingerprint, e.outcome) for e in rows(AlertEpisode)} == {
        ("d1", "rejected"),
        ("d3", "ignored"),
    }
    free_capacity()
    retried = deliver(client, batch)
    assert retried.json()["counts"] == {"duplicate": 2, "accepted": 1}
    accepted = episode("d2")
    intake_config.cache_clear()
    with TestClient(create_app()) as restarted:
        again = deliver(restarted, batch)
    assert again.json()["counts"] == {"duplicate": 3}
    assert episode("d2").run_id == accepted.run_id and count(Incident) == 1


def test_receipts_page_without_gaps_prune_and_never_store_secrets(client, intake, monkeypatch):
    with sessions()() as db, db.begin():
        moment = database_now(db)
        for index in range(60):
            db.add(
                AlertReceipt(
                    delivery_id=uuid4(),
                    received_at=moment,
                    source_id=SOURCE,
                    alert_status="firing",
                    fingerprint=f"{index:x}",
                    starts_at_key="k",
                    outcome="rejected",
                    reason="out_of_scope",
                )
            )
    sign_in(client, "viewer")
    seen, cursor = [], None
    while True:
        page = client.get("/api/alert-receipts", params={"cursor": cursor} if cursor else {})
        assert page.status_code == 200, page.text
        data = page.json()
        assert data["configured"] and data["source_id"] == SOURCE
        seen += [item["id"] for item in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 60
    bad = client.get("/api/alert-receipts", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_cursor"
    for elements in (
        ["2026-01-01T00:00:00+00:00", 1],
        [1, str(uuid4())],
        [None, None],
        [{}, []],
    ):
        cursor = base64.urlsafe_b64encode(json.dumps(elements).encode()).decode().rstrip("=")
        bad = client.get("/api/alert-receipts", params={"cursor": cursor})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_cursor"
    monkeypatch.setattr(intake_service, "RECEIPT_RETENTION", 10)
    assert deliver(client, envelope(alert("e9"))).status_code == 200
    kept = rows(AlertReceipt)
    assert len(kept) == 10 and any(r.fingerprint == "e9" for r in kept)
    listing = client.get("/api/alert-receipts").text
    with sessions()() as db:
        stored = json.dumps(
            [
                [str(v) for v in row]
                for table in ("alert_episodes", "alert_intake_receipts", "incidents")
                for row in db.execute(text(f"SELECT * FROM {table}"))
            ]
        )
    for secret in (TOKEN, READER_SECRET, ANNOTATION, "prometheus.invalid", "alertmanager.invalid"):
        assert secret not in stored and secret not in listing


def test_downgrade_refuses_to_discard_episode_bindings(client, intake):
    assert deliver(client, envelope(alert("c9"))).status_code == 200
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="Downgrade would discard alert episode"):
        command.downgrade(config, "0006_incident_requests")
    assert episode("c9").outcome == "accepted"
    assert isinstance(UUID(str(episode("c9").incident_id)), UUID)


def test_repeat_delivery_and_lifecycle_transitions_do_not_deadlock(client, intake):
    headers = sign_in(client)
    assert deliver(client, envelope(alert("a1"))).status_code == 200
    incident = episode("a1").incident_id
    app = create_app()

    def repeat(_):
        with TestClient(app) as other:
            return deliver(other, envelope(alert("a1"))).status_code

    def transition(action):
        return client.post(
            f"/api/incidents/{incident}/lifecycle",
            json={"action": action, "reason": "Concurrent lifecycle with a repeat delivery"},
            headers=headers,
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        for action in ("resolve", "reopen", "resolve"):
            repeats = [pool.submit(repeat, i) for i in range(2)]
            assert pool.submit(transition, action).result(timeout=30) == 200
            assert [future.result(timeout=30) for future in repeats] == [200, 200]
    with sessions()() as db:
        row = db.get(Incident, incident)
        assert row.state == "resolved" and row.alert_signal == "firing"
    assert count(InvestigationRun) == 1
