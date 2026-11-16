"""Fresh GET-only reads for recovery, outside retained investigation evidence."""

import asyncio
import os
from datetime import datetime, timezone
from datetime import timedelta
from dataclasses import replace

import httpx

from cloudops.features.investigations.collector import (
    CollectionContext,
    ScopeSnapshot,
    WindowSnapshot,
)
from cloudops.features.investigations.kubernetes_collector import (
    collect_kubernetes_events,
    collect_kubernetes_state,
)
from cloudops.features.investigations.prometheus import ReadFailure, parse_response, read_matrix
from cloudops.features.investigations.prometheus_config import load_config
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_reader import ObservationReader
from cloudops.features.workloads.observation_schemas import Identity
from cloudops.features.workloads.observation_settings import POLICY


def context(execution_id, incident_id, workload, start, end):
    return CollectionContext(
        execution_id,
        incident_id,
        workload.id,
        workload.cluster,
        workload.namespace,
        workload.uid,
        WindowSnapshot(start, end, "recovery_execution"),
        ScopeSnapshot(
            workload.cluster, workload.namespace, workload.name, workload.uid, workload.source
        ),
        "recovery-verification/v1",
        1,
        1,
    )


async def metrics(profile, ctx, before_call):
    try:
        config = load_config(os.environ["PROMETHEUS_READ_CONFIG"])
        tls = config.ssl_context()
    except (KeyError, OSError, ValueError):
        return {"error": "metrics_configuration_unavailable"}
    result = {}
    # Each query has its own bounded transport; no provider retry or zero filling.
    for key, spec in profile.queries.items():
        before_call()
        try:
            payload = await read_matrix(config, tls, spec, ctx, 4)
            parsed = parse_response(
                payload, config, spec, ctx, ctx.immutable_window, key, include_pod_uid=True
            )
            if parsed.foreign or parsed.invalid or parsed.empty or parsed.truncated:
                raise ReadFailure("incomplete_or_foreign_series")
            result[key] = {"series": parsed.series, "warnings": parsed.warnings}
        except ReadFailure as error:
            result[key] = {"error": error.reason}
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            result[key] = {"error": "metrics_read_unavailable"}
    return result


def capture(execution_id, incident_id, workload, start, end, *, before_call, profile=None):
    ctx = context(execution_id, incident_id, workload, start, end)
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "read_budgets": {
            "rollout_requests": POLICY.max_requests,
            "rollout_seconds": POLICY.deadline_seconds,
            "state_seconds": 8,
            "events_seconds": 8,
            "metric_queries": len(profile.queries) if profile is not None else 0,
            "metric_query_seconds": 4,
            "metric_response_bytes": 2 * 1024 * 1024,
        },
    }
    before_call()
    try:
        identity = Identity(
            workload_id=workload.id,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
            uid=workload.uid,
            epoch=workload.observation_epoch,
        )
        observation = asyncio.run(ObservationReader().read(identity, datetime.now(timezone.utc)))
        result["rollout"] = observation.model_dump(mode="json")
    except ObservationError as error:
        result["rollout"] = {"error": error.code}
    for key, adapter in (
        ("state", collect_kubernetes_state),
        ("events", collect_kubernetes_events),
    ):
        before_call()
        source = adapter(ctx, 8)
        result[key] = source.model_dump(mode="json")
    if profile is not None:
        metric_context = ctx
        if (end - start).total_seconds() == profile.window_seconds:
            metric_context = replace(
                ctx,
                immutable_window=WindowSnapshot(
                    start + timedelta(seconds=profile.warmup_seconds),
                    end,
                    "recovery_scoring",
                ),
            )
        result["metrics"] = asyncio.run(metrics(profile, metric_context, before_call))
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result
