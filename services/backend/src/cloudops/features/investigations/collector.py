"""Bounded source collection and fenced publication for investigation jobs."""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.kubernetes_collector import (
    collect_kubernetes_configmap_metadata,
    collect_kubernetes_events,
    collect_kubernetes_logs_current,
    collect_kubernetes_logs_previous,
    collect_kubernetes_state,
)
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.schemas import (
    Coverage,
    FixturePayload,
    SourceResult,
    SourceTimes,
)
from cloudops.features.investigations.states import TERMINAL_RUN
from cloudops.features.workloads.service import identity
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions


@dataclass(frozen=True)
class WindowSnapshot:
    start: datetime
    end: datetime
    source: str


@dataclass(frozen=True)
class ScopeSnapshot:
    cluster: str
    namespace: str
    name: str
    workload_uid: str
    source: str


@dataclass(frozen=True)
class CollectionContext:
    run_id: UUID
    incident_id: UUID
    workload_id: UUID
    cluster_id: str
    namespace: str
    workload_uid: str
    immutable_window: WindowSnapshot | None
    scope_snapshot: ScopeSnapshot
    configured_catalog_version: str
    attempt_number: int
    max_attempts: int


@dataclass(frozen=True)
class CollectionResult:
    workload_uid: str
    source_results: tuple[SourceResult, ...]


class TransientSourceError(Exception):
    """A transport or server failure eligible for the shared job retry budget."""


class SourceTimeout(TransientSourceError):
    """A read exceeded its supplied deadline."""


# Adapters must apply timeout_seconds to the actual underlying read; they must not
# start background calls which outlive that deadline or perform nested retries.
SourceAdapter = Callable[[CollectionContext, float], SourceResult]


def prepare(subject_id: UUID, *, attempt_number: int, max_attempts: int) -> CollectionContext:
    if not 1 <= attempt_number <= max_attempts <= 3:
        raise ValueError("Invalid trusted collection attempt budget.")
    with sessions()() as db:
        run = db.get(InvestigationRun, subject_id)
        from cloudops.features.incidents.service import get_incident

        incident = get_incident(db, run.incident_id)
        workload = identity(db, incident.workload_id)
        window = (
            WindowSnapshot(start=run.window_start, end=run.window_end, source=run.window_source)
            if run.window_start is not None and run.window_end is not None
            else None
        )
        return CollectionContext(
            run_id=run.id,
            incident_id=incident.id,
            workload_id=workload.id,
            cluster_id=workload.cluster,
            namespace=workload.namespace,
            workload_uid=workload.uid,
            immutable_window=window,
            scope_snapshot=ScopeSnapshot(
                workload.cluster, workload.namespace, workload.name, workload.uid, workload.source
            ),
            configured_catalog_version=CATALOG_VERSION,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )


def development_fixture(context: CollectionContext, timeout_seconds: float) -> SourceResult:
    delay = settings().collector_delay_seconds
    if delay > timeout_seconds:
        time.sleep(timeout_seconds)
        raise SourceTimeout()
    time.sleep(delay)
    if settings().collector_fail:
        raise RuntimeError("Development collector failure requested.")
    now = datetime.now(timezone.utc)
    scope = context.scope_snapshot
    payload = FixturePayload(
        label="Synthetic development evidence; no live cluster was contacted.",
        workload=f"{scope.cluster}/{scope.namespace}/{scope.name}",
        observation="Live workload health and telemetry are unavailable.",
    )
    return SourceResult(
        source_key="development_fixture",
        payload_kind="fixture",
        payload_version=1,
        coverage=Coverage(status="available", reason_codes=[]),
        source_times=SourceTimes(
            observed_at=now, collected_at=now, time_relation="current_observation"
        ),
        query_metadata=None,
        redacted_bounded_payload=payload.model_dump(),
    )


# Registration is deliberately finite. Adding a provider requires a code change,
# not an import path or an arbitrary callable from deployment configuration.
REGISTERED_ADAPTERS: dict[str, SourceAdapter] = {
    "development_fixture": development_fixture,
    "kubernetes.state": collect_kubernetes_state,
    "kubernetes.events": collect_kubernetes_events,
    "kubernetes.logs.current": collect_kubernetes_logs_current,
    "kubernetes.logs.previous": collect_kubernetes_logs_previous,
    "kubernetes.configmap.metadata": collect_kubernetes_configmap_metadata,
}


def collection_source_modes(mode: str) -> tuple[str, ...]:
    """Validate the deployment's provider selection without loading provider files."""
    names = tuple(name.strip() for name in mode.split(",") if name.strip())
    if not names or names == ("none",):
        return ()
    if len(names) != len(set(names)):
        raise ValueError("Duplicate collection source mode.")
    if "none" in names:
        raise ValueError("The none source mode cannot be combined with a provider.")
    if "development_fixture" in names and len(names) > 1:
        raise ValueError("The development fixture cannot be combined with a live provider.")
    if any(name not in _PROVIDERS for name in names):
        raise ValueError("Unknown collection source mode.")
    return names


def configured_adapters(mode: str) -> dict[str, SourceAdapter]:
    """Load the finite provider set for the collection runner."""
    adapters: dict[str, SourceAdapter] = {}
    for name in collection_source_modes(mode):
        selected = _PROVIDERS[name]()
        # The finite registry is also the validation authority used by both runners.
        # Register the provider's actual adapter objects so bindings remain identity-checked.
        REGISTERED_ADAPTERS.update(selected)
        adapters.update(selected)
    return adapters


def _prometheus_adapters() -> dict[str, SourceAdapter]:
    from cloudops.features.investigations import prometheus

    return prometheus.adapters()


_PROVIDERS = {
    "development_fixture": lambda: {
        "development_fixture": REGISTERED_ADAPTERS["development_fixture"]
    },
    "prometheus": _prometheus_adapters,
    "live_kubernetes": lambda: {
        key: REGISTERED_ADAPTERS[key]
        for key in (
            "kubernetes.state",
            "kubernetes.events",
            "kubernetes.logs.current",
            "kubernetes.logs.previous",
            "kubernetes.configmap.metadata",
        )
    },
}


def _unavailable(key: str, reason: str) -> SourceResult:
    return SourceResult(
        source_key=key,
        payload_kind=SOURCES[key].payload_kind,
        payload_version=1,
        coverage=Coverage(status="unavailable", reason_codes=[reason]),
        source_times=SourceTimes(
            observed_at=None, collected_at=datetime.now(timezone.utc), time_relation="not_captured"
        ),
        query_metadata=None,
        redacted_bounded_payload=None,
    )


def collect_one(
    context: CollectionContext,
    key: str,
    adapter: SourceAdapter | None,
    deadline: float,
    *,
    before_call: Callable[[], None] | None = None,
) -> SourceResult:
    """One source under the slice A policy; shared by the batch and checkpointed runners."""
    if adapter is None:
        return _unavailable(key, "source_not_configured")
    remaining = deadline - time.monotonic()
    try:
        if remaining <= 0:
            raise SourceTimeout()
        if before_call is not None:
            before_call()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceTimeout()
        result = adapter(context, min(5.0, remaining))
        if result.source_key != key or result.payload_kind != SOURCES[key].payload_kind:
            raise ValueError("Adapter returned a mismatched source result.")
        return result
    except (TransientSourceError, TimeoutError, ConnectionError) as exc:
        if context.attempt_number < context.max_attempts:
            raise
        reason = (
            "timeout" if isinstance(exc, (SourceTimeout, TimeoutError)) else "source_unavailable"
        )
        return _unavailable(key, reason)


def _validate(context: CollectionContext, adapters: Mapping[str, SourceAdapter]) -> None:
    if context.configured_catalog_version != CATALOG_VERSION:
        raise ValueError("Unknown collection catalog.")
    if not 1 <= context.attempt_number <= context.max_attempts <= 3:
        raise ValueError("Invalid trusted collection attempt budget.")
    for key, adapter in adapters.items():
        if key not in SOURCES or REGISTERED_ADAPTERS.get(key) is not adapter:
            raise ValueError("Unknown collection adapter binding.")


def collect(context: CollectionContext, adapters: Mapping[str, SourceAdapter]) -> CollectionResult:
    _validate(context, adapters)
    deadline = time.monotonic() + 90
    # Sequential reads intentionally use less than the four-read maximum. There
    # are no thread-based pseudo-timeouts or unbounded background provider calls.
    results = [collect_one(context, key, adapters.get(key), deadline) for key in SOURCES]
    return CollectionResult(context.workload_uid, tuple(results))


@dataclass(frozen=True)
class FinalizeCollection:
    """Every adapter read is checkpointed; the fenced finish derives coverage and pins.

    Slots without an installed adapter make no external call, so they are published as
    ``source_not_configured`` only in this final stage, never as partial failure evidence.
    """

    workload_uid: str
    unconfigured: tuple[str, ...] = ()
    checkpoint_stage: str = "finalize_collection"


class Interrupted(Exception):
    """The attempt lost its lease or dispatch permit; stop without another external call."""


def source_stage(key: str) -> str:
    return f"collect_source:{key}"


def collect_checkpointed(
    claim,
    context: CollectionContext,
    adapters: Mapping[str, SourceAdapter],
    gate,
    lease_lost: threading.Event | None = None,
) -> FinalizeCollection:
    """Collect missing sources one fenced checkpoint at a time; committed ones are reused."""
    from cloudops.features.investigations.storage import store_source
    from cloudops.shared.jobs import checkpoint, committed_stages, start_call

    _validate(context, adapters)
    done = committed_stages(claim)
    deadline = time.monotonic() + 90
    unconfigured = []
    # Match store_batch's canonical cross-source budget allocation order.
    for key in sorted(SOURCES):
        adapter = adapters.get(key)
        if adapter is None:
            unconfigured.append(key)
            continue
        stage = source_stage(key)
        if stage in done:
            continue

        def before_call():
            if (lease_lost is not None and lease_lost.is_set()) or not start_call(claim, gate):
                raise Interrupted()

        result = collect_one(context, key, adapter, deadline, before_call=before_call)

        def write(db, carry, result=result):
            return store_source(db, context.run_id, context.workload_uid, result, carry)

        if not checkpoint(claim, stage, gate, write):
            raise Interrupted()
    return FinalizeCollection(context.workload_uid, tuple(unconfigured))


class CollectionHandler:
    def __init__(self, finalize=None):
        self.finalize = finalize

    def eligible(self, db: Session, subject_id: UUID) -> bool:
        from cloudops.features.investigations.service import dispatch_blocker, terminalize
        from cloudops.features.recovery.service import invalidate

        run = db.scalar(
            select(InvestigationRun).where(InvestigationRun.id == subject_id).with_for_update()
        )
        if run.state in TERMINAL_RUN:
            return False
        code = dispatch_blocker(db, run)
        if code:
            terminalize(db, run, code)
            return False
        invalidate(db, run.incident_id)
        return True

    def started(self, db: Session, subject_id: UUID):
        run = db.get(InvestigationRun, subject_id)
        if run.state == "cancelled":
            return
        run.state, run.reason = "collecting", "Collecting configured evidence sources."

    def failed(self, db: Session, subject_id: UUID, reason: str, terminal: bool):
        run = db.get(InvestigationRun, subject_id)
        if run.state == "cancelled":
            return
        run.state = "failed" if terminal else "queued"
        run.reason = (
            reason if terminal else "Collection attempt failed. Waiting for a bounded retry."
        )
        run.outcome_code = "collection_failed" if terminal else None
        run.finished_at = database_now(db) if terminal else None

    def publish(self, db: Session, subject_id: UUID, result):
        from cloudops.features.investigations.storage import finalize_batch, store_batch

        run = db.get(InvestigationRun, subject_id)
        if run.state == "cancelled":
            return
        if isinstance(result, FinalizeCollection):
            if set(result.unconfigured) - SOURCES.keys():
                raise ValueError("Unknown catalog source.")
            finalize_batch(
                db,
                subject_id,
                result.workload_uid,
                [_unavailable(key, "source_not_configured") for key in result.unconfigured],
            )
        else:
            store_batch(db, subject_id, result.workload_uid, result.source_results)
        if self.finalize is not None:
            self.finalize(db, run)
            return
        run.state = "insufficient_evidence"
        run.outcome_code = "analysis_unavailable"
        run.reason = "Evidence collection saved. Analysis and recovery planning are unavailable."
        run.finished_at = database_now(db)
