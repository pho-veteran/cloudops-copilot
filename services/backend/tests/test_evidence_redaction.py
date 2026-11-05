"""Untrusted producers cannot persist arbitrary API objects or raw credentials."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from test_evidence_boundary import batch, setup_run

from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.schemas import Coverage
from cloudops.features.investigations.storage import store_batch
from cloudops.shared.database import sessions


def test_storage_redacts_before_hash_and_caps_state(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    index = next(i for i, r in enumerate(results) if r.source_key == "kubernetes.state")
    pods = [
        {"uid": f"pod-{i:02}", "name": "password=supersecret", "data": {"credential": "hidden"}}
        for i in range(15)
    ]
    results[index] = results[index].model_copy(
        update={
            "coverage": Coverage(status="available", reason_codes=[]),
            "redacted_bounded_payload": {"pods": list(reversed(pods)), "raw_secret": "hidden"},
        }
    )
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(r for r in rows if r.source_key == "kubernetes.state")
        evidence_id = row.id
        db.commit()
    with sessions()() as db:
        row = db.scalar(select(Evidence).where(Evidence.id == evidence_id))
        encoded = json.dumps(
            row.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert len(row.payload["pods"]) == 10
        assert b"supersecret" not in encoded and b"hidden" not in encoded
        assert b"[REDACTED]" in encoded
        assert row.sha256 == hashlib.sha256(encoded).hexdigest()
        assert row.coverage["status"] == "partial"


@pytest.mark.parametrize(
    ("source_key", "payload"),
    [
        ("kubernetes.state", {"pods": [{"uid": "b"}, {"uid": "a"}]}),
        (
            "kubernetes.events",
            {
                "events": [
                    {"uid": "z", "timestamp": "2026-11-01T00:00:00Z", "message": "First"},
                    {"uid": "a", "timestamp": "2026-11-01T00:01:00Z", "message": "Second"},
                ]
            },
        ),
        (
            "kubernetes.logs.current",
            {
                "logs": [
                    {
                        "pod_uid": "a",
                        "container": "app",
                        "text": "Authorization: Bearer synthetic-private-value\nNormal output",
                    }
                ]
            },
        ),
        (
            "prometheus.request_rate",
            {
                "series": [
                    {"name": "b", "samples": [[0, 1]]},
                    {"name": "a", "samples": [[0, 2]]},
                ]
            },
        ),
    ],
)
def test_ordering_and_redaction_preserve_complete_source_coverage(client, source_key, payload):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    _set_result(results, source_key, payload)
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(row for row in rows if row.source_key == source_key)
        assert row.coverage == {"status": "available", "reason_codes": []}
        assert "synthetic-private-value" not in json.dumps(row.payload)


def _set_result(results, key, payload):
    index = next(i for i, result in enumerate(results) if result.source_key == key)
    results[index] = results[index].model_copy(
        update={
            "coverage": Coverage(status="available", reason_codes=[]),
            "redacted_bounded_payload": payload,
        }
    )


def test_configmap_metadata_excludes_content_and_dropped_root_marks_partial(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    _set_result(
        results,
        "kubernetes.configmap.metadata",
        {
            "metadata": {
                "name": "config",
                "namespace": "ns",
                "uid": "id-123",
                "text": "private config contents",
                "data": {"password": "private"},
            },
            "content": "private content",
        },
    )
    _set_result(results, "kubernetes.events", {"events": [], "raw_api_object": {"private": "data"}})
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(r for r in rows if r.source_key == "kubernetes.configmap.metadata")
        assert row.payload == {"metadata": {"name": "config", "namespace": "ns", "uid": "id-123"}}
        assert row.coverage["status"] == "partial"
        assert "truncated" in row.coverage["reason_codes"]
        events = next(r for r in rows if r.source_key == "kubernetes.events")
        assert events.payload == {"events": []}
        assert events.coverage == {"status": "partial", "reason_codes": ["truncated"]}


def test_fixture_unknown_field_is_removed_with_explicit_truncation(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    original = next(
        result.redacted_bounded_payload
        for result in results
        if result.source_key == "development_fixture"
    )
    _set_result(results, "development_fixture", {**original, "discarded_field": "not stored"})
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(row for row in rows if row.source_key == "development_fixture")
        assert row.payload == original
        assert row.coverage == {"status": "partial", "reason_codes": ["truncated"]}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("é" * 17000, "é" * 16384), ("line\n" * 201, "line\n" * 200)],
    ids=["utf8-byte-limit-only", "line-limit-only"],
)
def test_log_trimming_alone_marks_coverage_partial(client, text, expected):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    _set_result(
        results,
        "kubernetes.logs.current",
        {"logs": [{"pod_uid": "a", "container": "app", "text": text}]},
    )
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(row for row in rows if row.source_key == "kubernetes.logs.current")
        assert row.payload["logs"][0]["text"] == expected
        assert row.coverage == {"status": "partial", "reason_codes": ["truncated"]}


def test_log_bounds_apply_across_segments_and_utf8_bytes(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    segment = ("é" * 200 + "\n") * 180
    _set_result(
        results,
        "kubernetes.logs.current",
        {
            "logs": [
                {"pod_uid": "pod-1", "container": "app", "text": segment},
                {"pod_uid": "pod-1", "container": "app", "lines": [segment, segment]},
            ]
        },
    )
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        row = next(r for r in rows if r.source_key == "kubernetes.logs.current")
        pieces = []
        for entry in row.payload["logs"]:
            pieces.extend(entry.get("lines", []))
            pieces.append(entry.get("text", ""))
        assert sum(len(piece.encode("utf-8")) for piece in pieces) <= 32768
        assert sum(len(piece.splitlines()) for piece in pieces) <= 200
        assert row.coverage["status"] == "partial"


def test_run_wide_samples_and_wrapped_resource_caps(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    for key in ("prometheus.request_rate", "prometheus.error_ratio", "prometheus.p95_latency"):
        _set_result(
            results,
            key,
            {
                "series": [
                    {"name": "one", "samples": [[i, i / 10] for i in range(9000)]},
                ]
            },
        )
    _set_result(
        results,
        "kubernetes.state",
        {
            "pods": [
                {"uid": "pod-1", "containers": [{"name": f"c{i:02}"} for i in range(25)]},
                {"uid": "pod-1", "containers": [{"name": f"c{i:02}"} for i in range(25, 40)]},
            ]
        },
    )
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        series = [
            r for r in rows if r.source_key.startswith("prometheus.") and r.payload is not None
        ]
        assert sum(len(s["samples"]) for r in series for s in r.payload["series"]) <= 20000
        state = next(r for r in rows if r.source_key == "kubernetes.state")
        containers = [c for pod in state.payload["pods"] for c in pod["containers"]]
        assert len(containers) <= 20
        assert state.coverage["status"] == "partial"


def test_missing_and_nonfinite_metric_samples_are_typed_gaps(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    _set_result(
        results,
        "prometheus.request_rate",
        {
            "series": [
                {"name": "one", "samples": [[1, 2.5], [2, float("nan")], [3, "+Inf"], [4, None]]}
            ]
        },
    )
    _set_result(
        results,
        "prometheus.error_ratio",
        {"series": [{"name": "one", "samples": [[1, float("inf")], [2, "NaN"]]}]},
    )
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, results)
        partial = next(r for r in rows if r.source_key == "prometheus.request_rate")
        missing = next(r for r in rows if r.source_key == "prometheus.error_ratio")
        assert partial.payload["series"][0]["samples"] == [[1, 2.5]]
        assert partial.coverage["status"] == "partial"
        assert "no_samples" in partial.coverage["reason_codes"]
        json.dumps(partial.payload, allow_nan=False)
        assert missing.payload is None and missing.sha256 is None
        assert missing.coverage["status"] == "unavailable"
        assert "no_samples" in missing.coverage["reason_codes"]


def test_query_metadata_is_bounded_before_publication(client):
    _, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    index = next(i for i, r in enumerate(results) if r.source_key == "prometheus.request_rate")
    results[index] = results[index].model_copy(
        update={
            "query_metadata": {"query_id": "request_rate", "query": "x" * 20000},
        }
    )
    with sessions()() as db, pytest.raises(ValueError, match="metadata exceeds"):
        store_batch(db, run_id, uid, results)


def test_authorization_and_quoted_credentials_never_enter_stored_content_or_digest(client):
    incident_id, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    secrets = ["synthetic-bearer", "synthetic-basic", "quoted password", "private-url"]
    text = (
        "Giữ nguyên thông điệp\n"
        f"Authorization: Bearer {secrets[0]}\n"
        f"Authorization: Basic {secrets[1]}\n"
        f'{{"password": "{secrets[2]}"}}\n'
        f"https://fixture:{secrets[3]}@example.invalid/path\n"
    )
    _set_result(
        results,
        "kubernetes.logs.current",
        {"logs": [{"pod_uid": "pod", "container": "app", "text": text}]},
    )
    with sessions()() as db, db.begin():
        rows = store_batch(db, run_id, uid, results)
        record = next(row for row in rows if row.source_key == "kubernetes.logs.current")
        evidence_id = record.id
    response = client.get(
        f"/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/content"
    )
    assert response.status_code == 200
    payload = response.json()["payload"]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert all(secret.encode() not in encoded for secret in secrets)
    assert payload["logs"][0]["text"].startswith("Giữ nguyên thông điệp\n")
    assert response.json()["sha256"] == hashlib.sha256(encoded).hexdigest()


def test_malformed_containers_cannot_bypass_metadata_and_event_whitelists(client):
    _, run_id, uid = setup_run(client)
    for key, payload in (
        ("kubernetes.configmap.metadata", {"metadata": "synthetic private configuration body"}),
        (
            "kubernetes.configmap.metadata",
            {"metadata": {"references": ["not an identifier\nprivate"]}},
        ),
        ("kubernetes.events", {"events": "synthetic raw API response"}),
    ):
        results = batch(datetime.now(timezone.utc))
        _set_result(results, key, payload)
        with sessions()() as db, pytest.raises(ValueError):
            store_batch(db, run_id, uid, results)


def test_query_metadata_cannot_smuggle_payloads_or_rebind_workload_identity(client):
    incident_id, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    index = next(i for i, row in enumerate(results) if row.source_key == "prometheus.request_rate")
    query = {
        "query_id": "request_rate",
        "logs": [{"text": "synthetic private source body"}],
        "source_identity": {
            "workload_uid": uid,
            "text": "synthetic private identity body",
        },
    }
    results[index] = results[index].model_copy(update={"query_metadata": query})
    with sessions()() as db, db.begin():
        store_batch(db, run_id, uid, results)
    response = client.get(f"/api/incidents/{incident_id}/runs/{run_id}")
    assert response.status_code == 200
    record = next(
        row for row in response.json()["evidence"] if row["source_key"] == "prometheus.request_rate"
    )
    assert record["query_metadata"] == {
        "query_id": "request_rate",
        "source_identity": {"workload_uid": uid},
    }
    assert "synthetic private" not in response.text
    results[index] = results[index].model_copy(
        update={
            "query_metadata": {
                "query_id": "request_rate",
                "source_identity": {"workload_uid": "other-uid"},
            }
        }
    )
    with sessions()() as db, pytest.raises(ValueError, match="identity mismatch"):
        store_batch(db, run_id, uid, results)


def test_missing_historical_window_cannot_become_a_live_log_window(client):
    _, run_id, uid = setup_run(client)
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, run_id)
        run.window_start = run.window_end = None
        run.window_source = "not_captured"
    results = batch(datetime.now(timezone.utc))
    _set_result(
        results,
        "kubernetes.logs.current",
        {
            "logs": [
                {"pod_uid": "pod", "container": "app", "text": "unbounded historical selection"}
            ]
        },
    )
    with sessions()() as db, pytest.raises(ValueError, match="captured request window"):
        store_batch(db, run_id, uid, results)
