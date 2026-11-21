"""Capture fixed provider settings and read only the selected workload."""

import base64
import hashlib
import json
import os
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from sqlalchemy import select

from cloudops.features.connections.schemas import (
    CheckTarget,
    Component,
    CoverageReason,
    Reason,
)
from cloudops.features.investigations.collector import WindowSnapshot
from cloudops.features.investigations.prometheus import (
    ReadFailure,
    matrix_coverage,
    parse_response,
    read_matrix,
)
from cloudops.features.investigations.prometheus_config import PrometheusConfig, load_config
from cloudops.features.workloads.models import ScopeState, ScopeTarget, Workload
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_reader import binding as kube_binding
from cloudops.features.workloads.observation_reader import read_deployment
from cloudops.shared.cluster_identity import cluster_fingerprint, server_identity
from cloudops.shared.config import settings

QUERY = "memory_working_set"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Selection:
    target: CheckTarget | None = None
    config_id: str | None = None
    key: str | None = None
    issue: Reason | None = None
    provider: dict | PrometheusConfig | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PreparedConfig:
    provider: dict | PrometheusConfig | None = field(default=None, repr=False)
    issue: Reason | None = None
    cluster_identity: str | None = None


@dataclass(frozen=True)
class MetricScope:
    namespace: str
    workload_uid: str
    immutable_window: WindowSnapshot


@dataclass(frozen=True)
class Result:
    reason: Reason
    coverage: Literal["available", "partial", "missing"] | None = None
    coverage_reasons: tuple[CoverageReason, ...] = ()
    evaluation_at: datetime | None = None


def provider_config(component):
    if component == "prometheus" and "prometheus" not in {
        mode.strip() for mode in os.environ.get("COLLECTION_SOURCE_MODE", "none").split(",")
    }:
        return None, "config_disabled"
    path = os.environ.get(
        "KUBE_READ_CONFIG" if component == "cluster" else "PROMETHEUS_READ_CONFIG"
    )
    if not path or not Path(path).is_file():
        return None, "config_missing"
    try:
        if component == "prometheus":
            return load_config(path), None
        with Path(path).open("rb") as handle:
            raw = handle.read(100_001)
        if len(raw) > 100_000:
            raise ValueError
        config = json.loads(raw)
        token = config["token"]
        if (
            not isinstance(token, str)
            or not 0 < len(token) <= 8192
            or any(not 33 <= ord(char) <= 126 for char in token)
        ):
            raise ValueError
        server_identity(config["server"])
        ca = base64.b64decode(config["ca_data"], validate=True).decode("ascii")
        ssl.create_default_context(cadata=ca)
        return config, None
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None, "config_invalid"


def prepare_config(component: Component) -> PreparedConfig:
    if component in ("database", "worker"):
        return PreparedConfig()

    provider, issue = provider_config(component)
    identity = None
    if isinstance(provider, dict):
        try:
            identity = cluster_fingerprint(provider)
        except (OSError, ValueError, TypeError):
            # Legacy scope supports CA bundles. A v2 pin requires a single certificate.
            pass
    return PreparedConfig(provider, issue, identity)


def selected(db, component: Component, prepared: PreparedConfig, *, fence=False) -> Selection:
    if component in ("database", "worker"):
        config_id = digest(
            [component, settings().worker_stale_seconds if component == "worker" else 1]
        )
        return Selection(config_id=config_id, key=config_id)

    scope = db.get(
        ScopeState, 1, with_for_update={"read": True} if fence else None, populate_existing=True
    )
    work = select(Workload).where(Workload.active, Workload.source == "live_kubernetes")
    scope_digest = scope.config_digest if scope else None
    v2 = scope is not None and scope.mode == "v2"
    if v2:
        work = work.join(ScopeTarget, ScopeTarget.workload_id == Workload.id).where(
            Workload.cluster == scope.cluster_key,
            ScopeTarget.configured,
            ScopeTarget.cluster == Workload.cluster,
            ScopeTarget.namespace == Workload.namespace,
            ScopeTarget.name == Workload.name,
        )
    workload = db.scalar(
        work.order_by(Workload.id).limit(1).execution_options(populate_existing=True)
    )
    target = (
        CheckTarget(
            workload_id=workload.id,
            uid=workload.uid,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
        )
        if workload
        else None
    )
    if prepared.issue:
        return Selection(target=target, issue=prepared.issue)
    if not target:
        return Selection(issue="scope_missing")
    if v2 or scope_digest is not None:
        try:
            with settings().workloads_file.open("rb") as handle:
                raw_scope = handle.read(100_001)
            if (
                len(raw_scope) > 100_000
                or (v2 and not scope.fingerprint)
                or hashlib.sha256(raw_scope).hexdigest() != scope_digest
            ):
                return Selection(target=target, issue="scope_changed")
        except OSError:
            return Selection(target=target, issue="scope_changed")

    config = prepared.provider
    if isinstance(config, PrometheusConfig):
        public = [config.model_dump(exclude={"token"}), QUERY]
        credential = config.token
    else:
        if v2:
            if not prepared.cluster_identity:
                return Selection(target=target, issue="config_invalid")
            if prepared.cluster_identity != scope.fingerprint:
                return Selection(target=target, issue="scope_changed")
        public = [kube_binding(config)]
        credential = config["token"]
    config_id = digest([component, scope_digest, public])
    key = digest([config_id, target.model_dump(mode="json"), credential])
    return Selection(target, config_id, key, provider=config)


PROVIDER_REASONS: dict[str, Reason] = {
    "permission_denied": "permission_denied",
    "forbidden": "permission_denied",
    "target_changed": "target_changed",
    "invalid_response": "invalid_response",
    "payload_limit": "response_limit",
    "truncated": "response_limit",
    "redirect_refused": "redirect_refused",
    "timeout": "timeout",
}


async def read(selection: Selection, started_at, deadline_at, *, transport=None) -> Result:
    target, provider = selection.target, selection.provider
    try:
        if monotonic() >= deadline_at:
            return Result("timeout")
        if isinstance(provider, dict):
            await read_deployment(
                provider,
                namespace=target.namespace,
                name=target.name,
                uid=target.uid,
                deadline_at=deadline_at,
                transport=transport,
            )
            return Result("deployment_read")
        window = WindowSnapshot(
            started_at - timedelta(seconds=4 * provider.step_seconds),
            started_at,
            "connection_check",
        )
        context = MetricScope(target.namespace, target.uid, window)
        spec = provider.queries[QUERY]
        ssl_context = provider.ssl_context()
        payload = await read_matrix(
            provider,
            ssl_context,
            spec,
            context,
            deadline_at - monotonic(),
            transport,
            deadline_at=deadline_at,
        )
        parsed = parse_response(payload, provider, spec, context, window, QUERY)
        if parsed.invalid and not parsed.series and not (parsed.foreign or parsed.empty):
            return Result("invalid_response")
        coverage = matrix_coverage(parsed, provider.step_seconds, window)
        reasons = tuple(coverage.reason_codes)
        if parsed.invalid:
            reasons += ("invalid_samples",)
        if coverage.status == "unavailable":
            return Result("metrics_missing", "missing", reasons)
        if coverage.status == "partial":
            reason = "metrics_stale" if "stale" in reasons else "metrics_partial"
            return Result(reason, "partial", reasons, parsed.observed_at)
        return Result("query_succeeded", "available", reasons, parsed.observed_at)
    except (ObservationError, ReadFailure) as error:
        return Result(PROVIDER_REASONS.get(str(error), "transport_unavailable"))
    except (TimeoutError, httpx.TimeoutException):
        return Result("timeout")
    except (httpx.HTTPError, OSError):
        return Result("transport_unavailable")
    except (ValueError, TypeError, RecursionError):
        return Result("invalid_response")
