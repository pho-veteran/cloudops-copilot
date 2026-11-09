import asyncio
import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cloudops.features.investigations import prometheus
from cloudops.features.investigations.collector import (
    CollectionContext,
    ScopeSnapshot,
    WindowSnapshot,
)
from cloudops.features.investigations.prometheus_config import (
    QUERY_IDS,
    PrometheusConfig,
    substitute_query,
)

CERT = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)
SCOPED_QUERY = (
    'sum(rate(cloudops_http_requests_total{namespace="{{namespace}}",'
    'deployment_uid="{{workload_uid}}"}[1m]))'
)


def config():
    return PrometheusConfig.model_validate(
        {
            "schema": "PrometheusReadConfig/v1",
            "endpoint_id": "lab-prometheus",
            "producer_version": "prometheus/v3.5.0",
            "server": "https://prometheus.test:8443/prometheus",
            "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
            "token": "private-token-sentinel",
            "step_seconds": 30,
            "binding": {
                "deployment_uid_label": "deployment_uid",
                "namespace_label": "namespace",
            },
            "queries": {
                query_id: {"query": SCOPED_QUERY, "unit": "count"} for query_id in QUERY_IDS
            },
        }
    )


def context(window=True, namespace="demo", uid="uid-1"):
    return CollectionContext(
        run_id=uuid4(),
        incident_id=uuid4(),
        workload_id=uuid4(),
        cluster_id="cluster",
        namespace=namespace,
        workload_uid=uid,
        immutable_window=WindowSnapshot(START, END, "preset_30") if window else None,
        scope_snapshot=ScopeSnapshot("cluster", namespace, "checkout", uid, "live_kubernetes"),
        configured_catalog_version="standard_v1",
        attempt_number=1,
        max_attempts=3,
    )


def metric(uid="uid-1", namespace="demo", **extra):
    return {
        "__name__": "cloudops_http_requests_total",
        "deployment_uid": uid,
        "namespace": namespace,
        **extra,
    }


def row(labels, values):
    return {"metric": labels, "values": values}


def matrix(rows, warnings=None):
    body = {"status": "success", "data": {"resultType": "matrix", "result": rows}}
    if warnings is not None:
        body["warnings"] = warnings
    return body


def samples():
    return [[START.timestamp(), "1"], [END.timestamp(), "2"]]


class Stream(httpx.AsyncByteStream):
    def __init__(self, body, delay=0):
        self.body, self.delay = body, delay

    async def __aiter__(self):
        for byte in self.body:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield bytes([byte])


def run(payload, handler=None, ctx=None, timeout=2.0):
    calls = []

    def send(request):
        calls.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, stream=Stream(json.dumps(payload).encode()))

    result = asyncio.run(
        prometheus.read_query(
            config(),
            config().ssl_context(),
            "request_rate",
            ctx or context(),
            timeout,
            httpx.MockTransport(send),
        )
    )
    return result, calls


def test_finite_matrix_is_available_and_provenanced():
    result, calls = run(matrix([row(metric(pod="checkout-1", container="app"), samples())]))
    assert len(calls) == 1
    assert calls[0].url.path == "/prometheus/api/v1/query_range"
    query = calls[0].url.params["query"]
    assert 'namespace="demo"' in query and 'deployment_uid="uid-1"' in query
    assert "{{" not in query
    assert calls[0].headers["authorization"] == "Bearer private-token-sentinel"
    assert result.coverage.status == "available"
    assert result.coverage.reason_codes == []
    assert result.payload_kind == "series" and result.payload_version == 1
    assert result.redacted_bounded_payload["series"][0]["uid"]
    assert result.redacted_bounded_payload["series"][0]["pod"] == "checkout-1"
    assert result.redacted_bounded_payload["series"][0]["samples"] == [
        [int(START.timestamp()), 1.0],
        [int(END.timestamp()), 2.0],
    ]
    assert result.source_times.time_relation == "within_requested_window"
    assert result.source_times.observed_at == END
    assert result.query_metadata["query_id"] == "request_rate"
    assert result.query_metadata["step"] == 30
    assert result.query_metadata["unit"] == "count"
    assert result.query_metadata["binding_version"] == "uid_label_v1"
    assert result.query_metadata["source_identity"]["workload_uid"] == "uid-1"
    assert result.query_metadata["window"]["start"] == START.isoformat()


def test_empty_and_non_finite_are_not_zero():
    empty, _ = run(matrix([]))
    assert empty.coverage.status == "unavailable"
    assert empty.coverage.reason_codes == ["no_samples"]
    assert empty.redacted_bounded_payload is None

    non_finite, _ = run(
        matrix([row(metric(), [[START.timestamp(), "NaN"], [END.timestamp(), "+Inf"]])])
    )
    assert non_finite.coverage.status == "unavailable"
    assert non_finite.coverage.reason_codes == ["no_samples"]
    assert non_finite.redacted_bounded_payload is None
    assert any("non-finite" in warning for warning in non_finite.query_metadata["warnings"])
    assert "NaN" not in non_finite.model_dump_json()
    assert "+Inf" not in non_finite.model_dump_json()


def test_partial_samples_keep_only_finite_values():
    result, _ = run(
        matrix(
            [
                row(
                    metric(),
                    [
                        [START.timestamp(), "1"],
                        [START.timestamp() + 30, "NaN"],
                        [END.timestamp(), "2"],
                    ],
                )
            ]
        )
    )
    assert result.coverage.status == "partial"
    assert result.coverage.reason_codes == ["no_samples"]
    assert result.redacted_bounded_payload["series"][0]["samples"] == [
        [int(START.timestamp()), 1.0],
        [int(END.timestamp()), 2.0],
    ]


@pytest.mark.parametrize(
    "labels",
    [
        metric(uid="old-uid"),
        metric(namespace="other"),
        {"__name__": "metric"},
        metric(deployment_uid="other"),
    ],
)
def test_foreign_or_unlabelled_series_are_rejected(labels):
    result, _ = run(matrix([row(labels, samples())]))
    assert result.coverage.status == "unavailable"
    assert result.coverage.reason_codes == ["ownership_unproven"]
    assert result.redacted_bounded_payload is None
    assert any("identity" in warning for warning in result.query_metadata["warnings"])


def test_foreign_series_do_not_hide_bound_series():
    result, _ = run(
        matrix(
            [
                row(metric(uid="old-uid"), samples()),
                row(metric(pod="checkout-1"), samples()),
            ]
        )
    )
    assert result.coverage.status == "partial"
    assert result.coverage.reason_codes == ["ownership_unproven"]
    assert len(result.redacted_bounded_payload["series"]) == 1


def test_series_truncation_is_deterministic():
    rows = [row(metric(pod=f"pod-{index:02}"), samples()) for index in range(30)]
    first, _ = run(matrix(rows))
    second, _ = run(matrix(list(reversed(rows))))
    assert first.coverage.status == "partial"
    assert first.coverage.reason_codes == ["truncated"]
    assert len(first.redacted_bounded_payload["series"]) == 25
    assert first.redacted_bounded_payload == second.redacted_bounded_payload


def test_stale_series_are_flagged_without_inventing_zero():
    result, _ = run(matrix([row(metric(), [[START.timestamp(), "3"]])]))
    assert result.coverage.status == "partial"
    assert result.coverage.reason_codes == ["stale"]
    assert result.redacted_bounded_payload["series"][0]["samples"] == [
        [int(START.timestamp()), 3.0]
    ]


def fractional_context():
    return replace(
        context(),
        immutable_window=WindowSnapshot(START, END + timedelta(milliseconds=123.456), "preset_30"),
    )


def test_prometheus_end_sample_rounding_is_inclusive():
    result, _ = run(
        matrix([row(metric(), [[START.timestamp(), "1"], [END.timestamp() + 0.1239, "2"]])]),
        ctx=fractional_context(),
    )
    assert result.coverage.status == "available"
    assert len(result.redacted_bounded_payload["series"][0]["samples"]) == 2


def test_sample_after_the_rounded_end_is_not_retained():
    result, _ = run(
        matrix([row(metric(), [[START.timestamp(), "1"], [END.timestamp() + 0.2, "2"]])]),
        ctx=fractional_context(),
    )
    assert result.coverage.status == "partial"
    assert "no_samples" in result.coverage.reason_codes
    assert len(result.redacted_bounded_payload["series"][0]["samples"]) == 1


@pytest.mark.parametrize(
    "response,reason",
    [
        (
            httpx.Response(403, stream=Stream(b'{"error":"private-provider-sentinel"}')),
            "forbidden",
        ),
        (
            httpx.Response(500, stream=Stream(b'{"error":"private-provider-sentinel"}')),
            "source_unavailable",
        ),
        (
            httpx.Response(
                200,
                stream=Stream(b"not-json"),
                headers={"content-type": "application/json"},
            ),
            "source_unavailable",
        ),
        (
            httpx.Response(200, stream=Stream(b"{}"), headers={"content-encoding": "gzip"}),
            "truncated",
        ),
    ],
)
def test_transport_failures_are_classified_and_sanitized(response, reason):
    result, calls = run(None, handler=lambda _: response)
    assert result.coverage.status == "unavailable"
    assert result.coverage.reason_codes == [reason]
    assert "private-provider-sentinel" not in result.model_dump_json()
    assert len(calls) == 1


def test_oversized_response_is_truncated(monkeypatch):
    monkeypatch.setattr(prometheus, "MAX_RESPONSE_BYTES", 20)
    result, calls = run(None, handler=lambda _: httpx.Response(200, stream=Stream(b"x" * 40)))
    assert result.coverage.status == "unavailable"
    assert result.coverage.reason_codes == ["truncated"]
    assert len(calls) == 1


def test_redirect_is_not_followed():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://foreign.test/secret"})

    result = asyncio.run(
        prometheus.read_query(
            config(),
            config().ssl_context(),
            "request_rate",
            context(),
            2.0,
            httpx.MockTransport(send),
        )
    )
    assert result.coverage.reason_codes == ["source_unavailable"]
    assert len(calls) == 1


def test_drip_feed_total_deadline_is_enforced():
    stream = Stream(b"{" * 100, delay=0.01)
    result = asyncio.run(
        prometheus.read_query(
            config(),
            config().ssl_context(),
            "request_rate",
            context(),
            0.05,
            httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        )
    )
    assert result.coverage.status == "unavailable"
    assert result.coverage.reason_codes == ["timeout"]


def test_missing_window_makes_no_request_and_is_not_captured():
    calls = []
    result = asyncio.run(
        prometheus.read_query(
            config(),
            config().ssl_context(),
            "request_rate",
            context(window=False),
            2.0,
            httpx.MockTransport(lambda request: calls.append(request)),
        )
    )
    assert calls == []
    assert result.coverage.reason_codes == ["not_captured"]
    assert result.redacted_bounded_payload is None


def test_remote_warnings_are_bounded_and_redacted():
    result, _ = run(
        matrix(
            [row(metric(), samples())],
            warnings=[
                "private-provider-sentinel " + "x" * 500,
                "second warning",
            ]
            + [f"warning-{index}" for index in range(20)],
        )
    )
    assert result.coverage.status == "available"
    warnings = result.query_metadata["warnings"]
    assert warnings == ["22 provider warning(s) were returned."]
    assert "private-provider-sentinel" not in result.model_dump_json()


def test_prometheus_config_rejects_unsupported_placeholder():
    with pytest.raises(ValueError):
        substitute_query('sum(metric{pod="{{pod_regex}}"}[1m])', "demo", "uid")
