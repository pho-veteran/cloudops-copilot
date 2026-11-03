"""Controlled transport evidence for alert identity bounds; not live cluster validation."""

import base64
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cloudops.features.incidents.intake_schemas import AlertmanagerEnvelope, canonical_start
from cloudops.features.workloads import alert_identity
from cloudops.features.workloads.alert_identity import (
    IdentityResolver,
    Refused,
    Resolved,
    ScopeSnapshot,
)
from cloudops.features.workloads.scope_config import cluster_fingerprint

CERT = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
WORKLOAD = uuid4()


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("2026-11-01T10:00:00Z", "2026-11-01T10:00:00.000000000Z"),
        ("2026-11-01T10:00:00.5Z", "2026-11-01T10:00:00.500000000Z"),
        ("2026-11-01t17:30:00.000000001+07:30", "2026-11-01T10:00:00.000000001Z"),
        ("2026-11-01T00:00:00.999999999-02:00", "2026-11-01T02:00:00.999999999Z"),
    ],
)
def test_canonical_start_keeps_nanoseconds_in_utc(value, key):
    assert canonical_start(value)[0] == key


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-01 10:00:00Z",
        "2026-11-01T10:00:00",
        "2026-11-01T10:00:00.1234567890Z",
        "2026-13-01T10:00:00Z",
        "2026-11-01T10:00:00+24:00",
        "0001-01-01T00:00:00Z",
    ],
)
def test_canonical_start_rejects_non_rfc3339_and_zero_time(value):
    with pytest.raises(ValueError):
        canonical_start(value)


def test_envelope_ignores_unknown_fields_and_bounds_alerts():
    alert = {
        "status": "firing",
        "labels": {"namespace": "demo"},
        "startsAt": "2026-11-01T10:00:00Z",
        "fingerprint": "abc",
        "unknownFutureField": True,
    }
    body = {"version": "4", "status": "firing", "alerts": [alert], "notification_reason": "x"}
    assert AlertmanagerEnvelope.model_validate(body).alerts[0].fingerprint == "abc"
    with pytest.raises(ValueError):
        AlertmanagerEnvelope.model_validate({**body, "alerts": [alert] * 101})
    with pytest.raises(ValueError):
        AlertmanagerEnvelope.model_validate(
            {**body, "alerts": [{**alert, "labels": {f"k{i}": "v" for i in range(65)}}]}
        )


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    config = {
        "server": "https://cluster.example:6443",
        "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
        "token": "reader-token",
    }
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("KUBE_READ_CONFIG", str(path))
    snapshot = ScopeSnapshot(
        True, cluster_fingerprint(config), targets={("demo", "checkout"): (WORKLOAD, "uid-1", 0)}
    )
    readers = []

    def build(handler):
        reader = IdentityResolver("lab", snapshot, transport=httpx.MockTransport(handler))
        readers.append(reader)
        return reader

    yield build
    for reader in readers:
        reader.close()


def deployment(uid="uid-1"):
    return {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "demo", "uid": uid}}


def test_cache_and_request_budget_bound_one_notification(resolver, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=deployment())

    reader = resolver(handler)
    labels = {"namespace": "demo", "deployment": "checkout"}
    assert reader.resolve(labels) == Resolved(WORKLOAD, "uid-1", "demo", "checkout", 0)
    assert reader.resolve(labels) == reader.resolve(labels) and len(calls) == 1
    monkeypatch.setattr(alert_identity, "MAX_REQUESTS", 1)
    exhausted = resolver(handler)
    exhausted.requests = 1
    assert exhausted.resolve(labels) == Refused("budget_exhausted", retry=True)


@pytest.mark.parametrize(
    "response",
    [
        lambda: httpx.Response(200, content=b"x" * (alert_identity.RESPONSE_BYTES + 1)),
        lambda: httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(deployment()).encode()),
            headers={"Content-Encoding": "br"},
        ),
        lambda: httpx.Response(200, json={"kind": "Deployment", "metadata": {"name": "other"}}),
        lambda: httpx.Response(200, content=b"not json"),
        lambda: httpx.Response(301, headers={"Location": "https://elsewhere.example/"}),
        lambda: httpx.Response(401),
    ],
)
def test_unreadable_or_redirected_reads_are_retryable(resolver, response):
    reader = resolver(lambda request: response())
    result = reader.resolve({"namespace": "demo", "deployment": "checkout"})
    assert result == Refused("cluster_unavailable", retry=True)


def test_reader_pin_mismatch_is_retryable_before_any_request(resolver, tmp_path):
    calls = []
    reader = resolver(lambda request: calls.append(request) or httpx.Response(200))
    reader.snapshot = ScopeSnapshot(True, "0" * 64, targets=reader.snapshot.targets)
    result = reader.resolve({"namespace": "demo", "deployment": "checkout"})
    assert result == Refused("cluster_unavailable", retry=True) and calls == []


def test_unpinned_scope_refuses_without_reads():
    reader = IdentityResolver("lab", ScopeSnapshot(False, None))
    try:
        assert reader.resolve({"namespace": "demo", "deployment": "checkout"}) == Refused(
            "scope_unpinned", retry=True
        )
    finally:
        reader.close()
