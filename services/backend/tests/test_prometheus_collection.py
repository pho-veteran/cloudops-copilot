"""Prometheus metric evidence persists through batch and checkpointed collection."""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from conftest import sign_in
from sqlalchemy import select
from test_access_admission import create, incident_body

from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.worker import HANDLERS
from cloudops.features.investigations import collector, prometheus
from cloudops.features.investigations.catalog import SOURCES
from cloudops.features.investigations.collector import (
    FinalizeCollection,
    collect_checkpointed,
    prepare,
)
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.prometheus_config import QUERY_IDS, PrometheusConfig
from cloudops.shared.database import sessions
from cloudops.shared.jobs import claim_next, finish

CERT = Path(__file__).resolve().parent / "fixtures/scope-ca.crt"
WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(minutes=30)
SCOPED_QUERY = (
    'sum(rate(cloudops_http_requests_total{namespace="{{namespace}}",'
    'deployment_uid="{{workload_uid}}"}[1m]))'
)


class Stream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def config():
    return PrometheusConfig.model_validate(
        {
            "schema": "PrometheusReadConfig/v1",
            "endpoint_id": "lab-prometheus",
            "producer_version": "prometheus/v3.5.0",
            "server": "https://prometheus.test:8443/prometheus",
            "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
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


def response_for(context):
    start = context.immutable_window.start.timestamp()
    end = context.immutable_window.end.timestamp()
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {
                        "__name__": "cloudops_http_requests_total",
                        "deployment_uid": context.workload_uid,
                        "namespace": context.namespace,
                        "pod": "checkout-1",
                        "container": "app",
                    },
                    "values": [
                        [start + offset, "1"] for offset in range(0, int(end - start) + 1, 30)
                    ],
                }
            ],
        },
    }


def adapters(calls):
    value = config()
    ssl_context = value.ssl_context()

    def build(query_id):
        def read(context, timeout):
            calls[query_id] = calls.get(query_id, 0) + 1
            body = json.dumps(response_for(context)).encode()
            transport = httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=Stream(body))
            )
            return asyncio.run(
                prometheus.read_query(value, ssl_context, query_id, context, timeout, transport)
            )

        return read

    return {f"prometheus.{query_id}": build(query_id) for query_id in QUERY_IDS}


@pytest.fixture
def metric_adapters(monkeypatch):
    calls = {}
    bound = adapters(calls)
    for key, adapter in bound.items():
        monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, adapter)
    return bound, calls


def evidence(run_id):
    with sessions()() as db:
        rows = db.scalars(select(Evidence).where(Evidence.run_id == UUID(run_id))).all()
        return {
            row.source_key: (row.sha256, row.payload, row.coverage, row.query_metadata)
            for row in rows
            if row.source_key.startswith("prometheus.")
        }


def set_window(run_id):
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, UUID(run_id))
        run.window_start = WINDOW_START
        run.window_end = WINDOW_END
        run.window_source = "explicit"


def test_batch_and_checkpointed_collection_publish_the_same_metric_evidence(
    client, metric_adapters
):
    bound, calls = metric_adapters
    headers = sign_in(client)

    batch_ids = create(client, headers, incident_body(client)).json()
    set_window(batch_ids["run_id"])
    batch_claim = claim_next(HANDLERS, GATE)
    batch_context = prepare(
        batch_claim.subject_id,
        attempt_number=batch_claim.attempt,
        max_attempts=batch_claim.max_attempts,
    )
    batch_result = collector.collect(batch_context, bound)
    assert finish(batch_claim, HANDLERS[batch_claim.kind], result=batch_result, gate=GATE)

    checkpoint_ids = create(client, headers, incident_body(client)).json()
    set_window(checkpoint_ids["run_id"])
    checkpoint_claim = claim_next(HANDLERS, GATE)
    checkpoint_context = prepare(
        checkpoint_claim.subject_id,
        attempt_number=checkpoint_claim.attempt,
        max_attempts=checkpoint_claim.max_attempts,
    )
    checkpoint_result = collect_checkpointed(checkpoint_claim, checkpoint_context, bound, GATE)
    assert isinstance(checkpoint_result, FinalizeCollection)
    before_retry = dict(calls)
    reused = collect_checkpointed(checkpoint_claim, checkpoint_context, bound, GATE)
    assert isinstance(reused, FinalizeCollection)
    assert calls == before_retry
    assert finish(
        checkpoint_claim,
        HANDLERS[checkpoint_claim.kind],
        result=reused,
        gate=GATE,
    )

    batch = evidence(batch_ids["run_id"])
    checkpointed = evidence(checkpoint_ids["run_id"])
    assert set(batch) == set(checkpointed) == {f"prometheus.{q}" for q in QUERY_IDS}
    for key in batch:
        assert batch[key][:3] == checkpointed[key][:3]
        assert batch[key][3]["query_id"] == key.removeprefix("prometheus.")
        assert batch[key][3]["source_identity"]["workload_uid"] == batch_context.workload_uid
        assert batch[key][2]["status"] == "available"
    assert sum(calls.values()) == 2 * len(QUERY_IDS)
    assert all(count == 2 for count in calls.values())


def test_metric_run_reads_through_the_api_with_traffic_coverage(client, metric_adapters):
    bound, _calls = metric_adapters
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    set_window(ids["run_id"])
    claim = claim_next(HANDLERS, GATE)
    context = prepare(
        claim.subject_id,
        attempt_number=claim.attempt,
        max_attempts=claim.max_attempts,
    )
    assert finish(
        claim,
        HANDLERS[claim.kind],
        result=collect_checkpointed(claim, context, bound, GATE),
        gate=GATE,
    )

    view = client.get(
        f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}", headers=headers
    ).json()
    assert view["state"] == "insufficient_evidence"
    assert view["outcome_code"] == "analysis_unavailable"
    assert {row["source_key"] for row in view["evidence"]} == set(SOURCES)
    packs = {row["pack"]: row["status"] for row in view["pack_coverage"]}
    assert packs["traffic_latency_errors"] == "available"
    request_rate = next(
        row for row in view["evidence"] if row["source_key"] == "prometheus.request_rate"
    )
    assert request_rate["content_status"] == "available"
    content = client.get(
        f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}"
        f"/evidence/{request_rate['id']}/content",
        headers=headers,
    ).json()
    series = content["payload"]["series"]
    assert len(series) == 1
    assert series[0]["unit"] == "count"
    assert series[0]["workload_uid"] == context.workload_uid
    assert len(series[0]["samples"]) == 61

    viewer = sign_in(client, "viewer")
    assert (
        client.get(
            f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}",
            headers=viewer,
        ).status_code
        == 200
    )
