"""Read-only history-lab checkpoint, executed inside the configured worker."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
import signal
import sys
import time
from uuid import UUID

if __name__ == "__main__":
    budget = 60 if "--full" in sys.argv[2:] else 25
    if "--deadline" in sys.argv[2:]:
        remaining = float(sys.argv[sys.argv.index("--deadline") + 1]) - time.time()
        if remaining <= 0:
            raise SystemExit(124)
        budget = min(budget, max(1, math.ceil(remaining)))
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(budget)

import httpx
from sqlalchemy import text

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.features.investigations.prometheus_config import escape_promql, load_config
from cloudops.features.recommendations.krr_protocol import metrics_attestation, metrics_binding
from cloudops.features.recommendations.producer import runner_key
from cloudops.features.recommendations.schemas import ReportWindow
from cloudops.features.recommendations.service import capture_identity, digest, prepare_report
from cloudops.features.recommendations.sources import SourceFailure, SourceReader, parse_matrix
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_service import detail
from cloudops.features.workloads.observation_settings import POLICY
from cloudops.shared.database import sessions


class ReadinessFailure(ValueError):
    pass


def traffic_rows(payload, uids, now):
    data = payload.get("data", {})
    rows = data.get("result")
    if (
        payload.get("status") != "success"
        or payload.get("warnings")
        or data.get("resultType") != "matrix"
        or not isinstance(rows, list)
        or len(rows) > 100
    ):
        raise ReadinessFailure("Traffic measurement unavailable or oversized")
    result = []
    for row in rows:
        labels = row["metric"]
        if labels.get("pod_uid") not in uids:
            continue
        points = row["values"]
        if (
            not points
            or len(points) > 181
            or any(
                len(pair) != 2
                or any(isinstance(v, bool) for v in pair)
                or not all(math.isfinite(float(v)) and float(v) >= 0 for v in pair)
                or not now - 180 <= float(pair[0]) <= now
                for pair in points
            )
            or now - float(points[-1][0]) > 120
            or any(
                float(b[0]) <= float(a[0]) or float(b[1]) < float(a[1])
                for a, b in zip(points, points[1:])
            )
        ):
            raise ReadinessFailure("Fresh finite traffic measurements required")
        result.append(
            {
                "labels": {k: v for k, v in labels.items() if k in {"pod_uid", "status", "le"}},
                "samples": points,
            }
        )
    if {r["labels"]["pod_uid"] for r in result} != uids:
        raise ReadinessFailure("Current Pod traffic metric coverage is missing")
    return result


async def checkpoint(workload_id, *, full=False):
    os.environ["PGCONNECT_TIMEOUT"] = "5"
    with sessions()() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        workload = db.get(Workload, UUID(workload_id))
        if not workload or not workload.active or workload.source != "live_kubernetes":
            raise ReadinessFailure("Active live workload required")
        observation = detail(db, workload.id)
        if (
            observation.summary.freshness != "fresh"
            or observation.summary.coverage != "complete"
            or observation.summary.effective_health != "healthy"
            or not observation.current
        ):
            raise ReadinessFailure("Fresh complete healthy ownership observation required")
        identity = capture_identity(db, workload)
        profile = prepare_report(db, workload, None).profile
    if not all(
        (
            identity.cluster_fingerprint,
            identity.template_digest,
            identity.resources_digest,
            identity.pod_template_digest,
            identity.containers,
            profile["image_digest"],
        )
    ):
        raise ReadinessFailure("Pinned producer and full identity/template proof required")
    if POLICY.history_count * POLICY.poll_seconds < 22680 or POLICY.history_days < 1:
        raise ReadinessFailure("Ownership retention cannot cover the finite history recipe")
    pods = [p for p in observation.current.pods if not p.excluded and not p.terminating]
    if not pods or any(
        p.phase != "Running"
        or not p.ready
        or any(
            c.kind != "regular" or c.state != "running" or c.restart_count != 0
            for c in p.containers
        )
        for p in pods
    ):
        raise ReadinessFailure("Stable healthy Pods without restarts required before qualification")
    now = datetime.now(timezone.utc)
    config = load_config(os.environ["PROMETHEUS_READ_CONFIG"])
    attestation = metrics_attestation(
        metrics_binding(config.model_dump(by_alias=True)), runner_key()
    )
    output = {
        "observed_at": now.isoformat(),
        "identity": identity.model_dump(mode="json", exclude={"observation_id", "observed_at"}),
        "profile": profile,
        "metrics_attestation": attestation,
        "observation_at": observation.current.observed_at.isoformat(),
        "pods": sorted((p.model_dump(mode="json") for p in pods), key=lambda p: p["uid"]),
        "retention": {"count": POLICY.history_count, "days": POLICY.history_days},
    }
    if full:
        start = now.replace(second=0, microsecond=0) - timedelta(hours=6)
        window = ReportWindow(
            kind="query_envelope",
            start=start,
            end=now,
            raw_input_start=start - timedelta(seconds=75),
            raw_input_end=now,
        )
        source = await SourceReader().read(identity, window)
        if (
            source.before["metadata"]["uid"] != identity.uid
            or source.before["spec"]["template"] != source.after["spec"]["template"]
            or digest(source.before["spec"]["template"]) != identity.pod_template_digest
            or any(source.controllers.values())
            or metrics_attestation(source.metrics_source_sha256, runner_key()) != attestation
        ):
            raise ReadinessFailure("Current source/controller/producer binding is not ready")
        output["source_prerequisites"] = {
            "query_digest": source.query_digest,
            "config_digest": source.config_digest,
            "sample_counts": {
                name: sum(len(s.samples) for s in rows) for name, rows in source.series.items()
            },
            "coverage_qualified": False,
        }
    namespace, uid = escape_promql(identity.namespace), escape_promql(identity.uid)
    queries = {
        "cpu": 'container_cpu_usage_seconds_total{namespace="' + namespace + '"}[180s]',
        "memory": 'container_memory_working_set_bytes{namespace="' + namespace + '"}[180s]',
        **{
            name: name + '{namespace="' + namespace + '",deployment_uid="' + uid + '"}[180s]'
            for name in (
                "lab_http_requests_total",
                "lab_http_request_duration_seconds_sum",
                "lab_http_request_duration_seconds_count",
                "lab_http_request_duration_seconds_bucket",
            )
        },
    }
    headers = {"Accept-Encoding": "identity"}
    if config.token:
        headers["Authorization"] = "Bearer " + config.token
    async with (
        asyncio.timeout(15),
        httpx.AsyncClient(
            verify=config.ssl_context(),
            trust_env=False,
            follow_redirects=False,
            timeout=8,
        ) as client,
    ):

        async def read(name, query):
            async with client.stream(
                "GET",
                config.server + "/api/v1/query",
                headers=headers,
                params={"query": query, "time": now.timestamp()},
            ) as response:
                if response.status_code != 200:
                    raise ReadinessFailure("Measured source read failed")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 512_000:
                        raise ReadinessFailure("Measured source response exceeded its bound")
            payload = json.loads(raw)
            if name in {"cpu", "memory"}:
                rows = parse_matrix(
                    payload, identity.namespace, now.timestamp() - 180, now.timestamp(), [0]
                )
                wanted = {(p.uid, c.name) for p in pods for c in p.containers}
                names = {(p.name, c.name): p.uid for p in pods for c in p.containers}
                if any(
                    (s.labels.get("pod"), s.labels.get("container")) in names
                    and s.labels.get("pod_uid")
                    != names[(s.labels.get("pod"), s.labels.get("container"))]
                    for s in rows
                ):
                    raise ReadinessFailure("Current Pod name has a missing or foreign metric UID")
                rows = [
                    s
                    for s in rows
                    if (s.labels.get("pod_uid"), s.labels.get("container")) in wanted
                ]
                if (
                    len(rows) != len(wanted)
                    or {(s.labels.get("pod_uid"), s.labels.get("container")) for s in rows}
                    != wanted
                ):
                    raise ReadinessFailure("Current Pod/container metric coverage is missing")
                samples = [{"labels": s.labels, "samples": s.samples} for s in rows]
            else:
                samples = traffic_rows(payload, {p.uid for p in pods}, now.timestamp())
            return name, samples

        output["measurements"] = dict(
            await asyncio.gather(*(read(n, q) for n, q in queries.items()))
        )
    if (
        metrics_attestation(
            metrics_binding(
                load_config(os.environ["PROMETHEUS_READ_CONFIG"]).model_dump(by_alias=True)
            ),
            runner_key(),
        )
        != attestation
    ):
        raise ReadinessFailure("Metrics configuration changed during checkpoint")
    return output


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(checkpoint(sys.argv[1], full="--full" in sys.argv[2:]))))
    except Exception as error:
        reason = (
            str(error)
            if isinstance(error, ReadinessFailure)
            else error.reason
            if isinstance(error, SourceFailure)
            else "Source readiness failed"
        )
        print(json.dumps({"error": reason}))
        raise SystemExit(1) from None
