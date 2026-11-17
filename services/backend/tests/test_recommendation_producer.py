"""Real PostgreSQL publication with explicitly controlled runner/source transports."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import UUID

import pytest
from recommendation_fixtures import runner_case
from test_prometheus_collection import config
from test_recommendation_reports import create
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.recommendations import producer
from cloudops.features.recommendations.krr_protocol import digest, normalize, snapshot
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.worker import ReportHandler
from cloudops.shared.database import sessions
from cloudops.shared.jobs import claim_next, finish

permission_context = _permission_context


@pytest.fixture
def prepared(permission_context, tmp_path, monkeypatch):
    case, body, raw, observed = runner_case()
    path = tmp_path / "prometheus.json"
    path.write_text(config().model_dump_json(by_alias=True))
    monkeypatch.setenv("PROMETHEUS_READ_CONFIG", str(path))
    key = tmp_path / "runner-key"
    key.write_text("controlled-publication-key")
    monkeypatch.setenv("KRR_RUNNER_KEY", str(key))
    monkeypatch.setenv("KRR_RUNNER_IMAGE_DIGEST", body["profile"]["image_digest"])
    rid = UUID(create(permission_context).json()["id"])
    with sessions()() as db, db.begin():
        row = db.get(RecommendationReport, rid)
        body["identity"]["workload_id"] = str(row.workload_id)
        row.input_identity, row.profile = body["identity"], body["profile"]
    return (
        permission_context,
        rid,
        case,
        normalize(raw, observed, {**body, "identity": case[0].model_dump(mode="json")}),
    )


def response(body, *, completed=False, reason="runner_source_unavailable", containers=()):
    current = snapshot(runner_case()[0][3].before)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "protocol_version": 1,
        "request_sha256": digest(body),
        "metrics_source_sha256": body["metrics_source_sha256"],
        "state": "completed" if completed else "unavailable",
        "reason": reason,
        "tool_version": "1.31.1",
        "image_digest": body["profile"]["image_digest"],
        "invocation_started_at": now if completed else None,
        "invocation_finished_at": now if completed else None,
        "tool_exit_code": 0 if completed else None,
        "timed_out": False,
        "output_sha256": "c" * 64 if completed else None,
        "scope_before": current if completed else None,
        "scope_after": current if completed else None,
        "containers": list(containers),
    }


def publish(c, rid, result):
    claim = claim_next({"recommendation_report_v1": ReportHandler()}, GATE)
    assert claim.subject_id == rid
    assert finish(claim, ReportHandler(), result=result, gate=GATE)
    exported = c.actors["viewer"].client.get(f"/api/recommendation-reports/{rid}/export")
    assert exported.status_code == 200
    saved = json.loads(exported.content)
    assert saved["result_v2"]["state"] == "unavailable" and saved["result_v2"]["resources"] == []
    assert (
        c.actors["viewer"].client.get(f"/api/recommendation-reports/{rid}/export").content
        == exported.content
    )
    return saved


@pytest.mark.parametrize(
    "reason",
    [
        "runner_target_changed",
        "tool_output_invalid",
        "runner_payload_limit",
        "runner_source_unavailable",
        "private-unknown-reason",
    ],
)
def test_sanitized_runner_failure_reason_survives_generate_and_fenced_export(
    prepared, monkeypatch, reason
):
    c, rid, _, _ = prepared
    monkeypatch.setattr(
        producer, "call_runner", lambda body, **kwargs: response(body, reason=reason)
    )
    monkeypatch.setattr(
        producer,
        "SourceReader",
        lambda: pytest.fail("A failed tool must not collect source evidence"),
    )
    result = producer.generate(rid, before_call=lambda: None)
    expected = "runner_failed" if reason == "private-unknown-reason" else reason
    assert result.reason == expected
    saved = publish(c, rid, result)
    assert saved["result_v2"]["reason"] == expected
    assert "private-unknown" not in json.dumps(saved)


@pytest.mark.parametrize("boundary", ["runner", "source"])
def test_same_identity_different_metrics_source_cannot_publish_numbers(
    prepared, monkeypatch, boundary
):
    c, rid, case, containers = prepared

    def runner(body, **kwargs):
        value = response(body, completed=True, containers=containers)
        if boundary == "runner":
            value["metrics_source_sha256"] = "f" * 64
        return value

    class DifferentSource:
        async def read(self, identity, window):
            assert boundary == "source"
            return replace(case[3], metrics_source_sha256="f" * 64)

    monkeypatch.setattr(producer, "call_runner", runner)
    monkeypatch.setattr(producer, "SourceReader", DifferentSource)
    monkeypatch.setattr(producer, "load_history", lambda *args: case[2])
    result = producer.generate(rid, before_call=lambda: None)
    assert result.reason == (
        "tool_output_invalid" if boundary == "runner" else "runner_metrics_source_changed"
    )
    publish(c, rid, result)
