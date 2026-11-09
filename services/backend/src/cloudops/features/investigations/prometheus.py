"""Bounded Prometheus metric reads for the standard investigation catalog."""

import asyncio
import hashlib
import json
import math
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import httpx

from cloudops.features.investigations.catalog import CATALOG_VERSION
from cloudops.features.investigations.collector import CollectionContext, SourceAdapter
from cloudops.features.investigations.prometheus_config import (
    QUERY_IDS,
    PrometheusConfig,
    QuerySpec,
    load_config,
    substitute_query,
)
from cloudops.features.investigations.schemas import Coverage, SourceResult, SourceTimes

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SERIES = 25
MAX_WARNINGS = 16
WARNING_CHARS = 240
QUERY_VERSION = CATALOG_VERSION
BINDING_VERSION = "uid_label_v1"
QUERY_NAMES = {
    "request_rate": "Request rate",
    "error_ratio": "Error ratio",
    "p95_latency": "p95 latency",
    "memory_working_set": "Memory working set",
    "memory_limit": "Memory limit",
    "restarts": "Container restarts",
}


class ReadFailure(Exception):
    """A classified read failure; the message never contains provider content."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Parsed:
    series: list[dict]
    warnings: list[str]
    observed_at: datetime | None
    foreign: int
    invalid: int
    empty: int
    truncated: bool


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _warning(value: str) -> str | None:
    cleaned = " ".join(
        "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split()
    )
    if not cleaned:
        return None
    return cleaned[:WARNING_CHARS]


def _warnings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = _warning(value)
        if cleaned and cleaned not in result:
            result.append(cleaned)
        if len(result) >= MAX_WARNINGS:
            break
    return result


def _series_uid(metric: dict) -> str:
    return hashlib.sha256(_canonical(metric).encode("utf-8")).hexdigest()


def _text_label(metric: dict, key: str) -> str | None:
    value = metric.get(key)
    if isinstance(value, str) and 0 < len(value) <= 253 and value.isprintable():
        return value
    return None


def _timestamp(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _sample_value(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _observed_at(samples: list[list]) -> datetime | None:
    if not samples:
        return None
    try:
        return datetime.fromtimestamp(float(samples[-1][0]), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _sample_rows(values, start: float, end: float) -> tuple[list[list], int]:
    rows: list[list] = []
    invalid = 0
    if not isinstance(values, list):
        return rows, 1
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            invalid += 1
            continue
        timestamp, raw = pair
        moment = _timestamp(timestamp)
        number = _sample_value(raw)
        if moment is None or number is None or moment < start or moment > end:
            invalid += 1
            continue
        rows.append([int(moment) if moment.is_integer() else moment, number])
    rows.sort(key=lambda row: row[0])
    return rows, invalid


def parse_response(
    payload,
    config: PrometheusConfig,
    spec: QuerySpec,
    context: CollectionContext,
    window,
    query_id: str,
) -> Parsed:
    """Validate one Prometheus matrix response and project only bound series."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ReadFailure("source_unavailable")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "matrix":
        raise ReadFailure("source_unavailable")
    result = data.get("result")
    if not isinstance(result, list):
        raise ReadFailure("source_unavailable")

    remote_warnings = payload.get("warnings")
    warnings = (
        [f"{len(remote_warnings)} provider warning(s) were returned."]
        if isinstance(remote_warnings, list) and remote_warnings
        else []
    )
    start = math.floor(window.start.timestamp() * 1000) / 1000
    end = math.ceil(window.end.timestamp() * 1000) / 1000
    foreign = invalid = empty = 0
    series: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            invalid += 1
            continue
        metric = item.get("metric")
        if not isinstance(metric, dict):
            invalid += 1
            continue
        if (
            metric.get(config.binding.deployment_uid_label) != context.workload_uid
            or metric.get(config.binding.namespace_label) != context.namespace
        ):
            foreign += 1
            continue
        rows, bad = _sample_rows(item.get("values", item.get("samples")), start, end)
        invalid += bad
        if not rows:
            empty += 1
            continue
        projected = {
            "uid": _series_uid(metric),
            "name": _text_label(metric, "__name__") or QUERY_NAMES.get(query_id, spec.unit),
            "namespace": context.namespace,
            "workload_uid": context.workload_uid,
            "unit": spec.unit,
            "samples": rows,
        }
        pod = _text_label(metric, "pod")
        container = _text_label(metric, "container")
        if pod:
            projected["pod"] = pod
        if container:
            projected["container"] = container
        series.append(projected)

    series.sort(key=lambda row: row["uid"])
    truncated = len(series) > MAX_SERIES
    series = series[:MAX_SERIES]
    if foreign:
        warnings.append(
            f"{foreign} returned series did not match the configured workload identity."
        )
    if invalid:
        warnings.append(f"{invalid} malformed or non-finite sample entries were omitted.")
    if empty:
        warnings.append(f"{empty} matching series had no finite samples in the window.")
    if truncated:
        warnings.append(f"Series were truncated to {MAX_SERIES}.")
    moments = [moment for item in series if (moment := _observed_at(item["samples"])) is not None]
    observed_at = max(moments) if moments else None
    return Parsed(
        series=series,
        warnings=_warnings(warnings),
        observed_at=observed_at,
        foreign=foreign,
        invalid=invalid,
        empty=empty,
        truncated=truncated,
    )


def _query_metadata(
    config: PrometheusConfig,
    spec: QuerySpec,
    context: CollectionContext,
    query_id: str,
    template: str,
    resolved: str | None,
    warnings: list[str],
) -> dict:
    window = context.immutable_window
    metadata = {
        "query_id": query_id,
        "query_version": QUERY_VERSION,
        "binding_version": BINDING_VERSION,
        "producer_version": config.producer_version,
        "query": template,
        "step": config.step_seconds,
        "unit": spec.unit,
        "warnings": warnings,
        "source_identity": {
            "endpoint_id": config.endpoint_id,
            "cluster_id": context.cluster_id,
            "namespace": context.namespace,
            "workload_uid": context.workload_uid,
        },
    }
    if resolved is not None:
        metadata["resolved_query"] = resolved
    if window is not None:
        metadata["window"] = {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
        }
    return metadata


def _result(
    config: PrometheusConfig,
    spec: QuerySpec,
    context: CollectionContext,
    query_id: str,
    template: str,
    resolved: str | None,
    coverage: Coverage,
    payload: dict | None,
    observed_at: datetime | None,
    warnings: list[str],
) -> SourceResult:
    window = context.immutable_window
    return SourceResult(
        source_key=f"prometheus.{query_id}",
        payload_kind="series",
        payload_version=1,
        coverage=coverage,
        source_times=SourceTimes(
            observed_at=observed_at,
            collected_at=datetime.now(timezone.utc),
            time_relation="within_requested_window" if window is not None else "not_captured",
        ),
        query_metadata=_query_metadata(
            config, spec, context, query_id, template, resolved, warnings
        ),
        redacted_bounded_payload=payload,
    )


def _failure(
    config: PrometheusConfig,
    spec: QuerySpec,
    context: CollectionContext,
    query_id: str,
    template: str,
    resolved: str | None,
    reason: str,
) -> SourceResult:
    return _result(
        config,
        spec,
        context,
        query_id,
        template,
        resolved,
        Coverage(status="unavailable", reason_codes=[reason]),
        None,
        None,
        [],
    )


def _coverage(parsed: Parsed, step_seconds: int, window) -> Coverage:
    if not parsed.series:
        reason = "ownership_unproven" if parsed.foreign else "no_samples"
        return Coverage(status="unavailable", reason_codes=[reason])
    reasons: set[str] = set()
    if parsed.foreign:
        reasons.add("ownership_unproven")
    if parsed.invalid or parsed.empty:
        reasons.add("no_samples")
    if parsed.truncated:
        reasons.add("truncated")
    first_samples = [item["samples"][0][0] for item in parsed.series if item["samples"]]
    if first_samples and min(first_samples) > window.start.timestamp() + step_seconds + 1:
        reasons.add("no_samples")
    if parsed.observed_at is not None:
        end = window.end.timestamp()
        if parsed.observed_at.timestamp() < end - step_seconds:
            reasons.add("stale")
    return Coverage(status="partial" if reasons else "available", reason_codes=sorted(reasons))


async def _read(
    config: PrometheusConfig,
    ssl_context: ssl.SSLContext,
    query_id: str,
    context: CollectionContext,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    window = context.immutable_window
    if window is None:
        raise ReadFailure("not_captured")
    if timeout_seconds <= 0:
        raise ReadFailure("timeout")
    query_spec = config.queries[query_id]
    query = substitute_query(query_spec.query, context.namespace, context.workload_uid)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    params = {
        "query": query,
        "start": window.start.timestamp(),
        "end": window.end.timestamp(),
        "step": config.step_seconds,
    }
    async with asyncio.timeout(timeout_seconds):
        async with httpx.AsyncClient(
            verify=ssl_context,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(min(4.0, timeout_seconds)),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers=headers,
            transport=transport,
        ) as client:
            async with client.stream("GET", config.query_url(), params=params) as response:
                if response.status_code in (401, 403):
                    raise ReadFailure("forbidden")
                if response.status_code != 200:
                    raise ReadFailure("source_unavailable")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ReadFailure("truncated")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ReadFailure("truncated")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise ReadFailure("source_unavailable") from None
    return payload


async def read_query(
    config: PrometheusConfig,
    ssl_context: ssl.SSLContext,
    query_id: str,
    context: CollectionContext,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SourceResult:
    """Read one fixed metric query; never raises provider content to the caller."""
    if query_id not in QUERY_IDS:
        raise ValueError("Unknown metric query.")
    query_spec = config.queries[query_id]
    window = context.immutable_window
    resolved = (
        substitute_query(query_spec.query, context.namespace, context.workload_uid)
        if window is not None
        else None
    )
    try:
        payload = await _read(config, ssl_context, query_id, context, timeout_seconds, transport)
        parsed = parse_response(payload, config, query_spec, context, window, query_id)
    except ReadFailure as error:
        return _failure(
            config, query_spec, context, query_id, query_spec.query, resolved, error.reason
        )
    except (TimeoutError, httpx.TimeoutException):
        return _failure(
            config, query_spec, context, query_id, query_spec.query, resolved, "timeout"
        )
    except (httpx.HTTPError, OSError, ssl.SSLError, ValueError, TypeError):
        return _failure(
            config,
            query_spec,
            context,
            query_id,
            query_spec.query,
            resolved,
            "source_unavailable",
        )
    coverage = _coverage(parsed, config.step_seconds, window)
    return _result(
        config,
        query_spec,
        context,
        query_id,
        query_spec.query,
        resolved,
        coverage,
        {"series": parsed.series} if parsed.series else None,
        parsed.observed_at,
        parsed.warnings,
    )


def _adapter(
    config: PrometheusConfig,
    ssl_context: ssl.SSLContext,
    query_id: str,
) -> SourceAdapter:
    def read(context: CollectionContext, timeout_seconds: float) -> SourceResult:
        return asyncio.run(read_query(config, ssl_context, query_id, context, timeout_seconds))

    return read


@lru_cache(maxsize=4)
def _adapters_for(path: str) -> dict[str, SourceAdapter]:
    config = load_config(path)
    context = config.ssl_context()
    return {f"prometheus.{query_id}": _adapter(config, context, query_id) for query_id in QUERY_IDS}


def adapters(path: str | None = None) -> dict[str, SourceAdapter]:
    """Build the six finite metric adapters from the trusted read configuration."""
    selected = path or os.environ.get("PROMETHEUS_READ_CONFIG")
    if not selected:
        raise ValueError("PROMETHEUS_READ_CONFIG is required when Prometheus is enabled.")
    return dict(_adapters_for(str(Path(selected).resolve())))


def clear_cache() -> None:
    _adapters_for.cache_clear()
