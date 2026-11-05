"""Bounded source collection and fenced publication for investigation jobs."""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
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
REGISTERED_ADAPTERS: dict[str, SourceAdapter] = {"development_fixture": development_fixture}


def configured_adapters(mode: str) -> dict[str, SourceAdapter]:
    if mode == "none":
        return {}
    if mode == "development_fixture":
        return {"development_fixture": REGISTERED_ADAPTERS["development_fixture"]}
    raise ValueError("Unknown collection source mode.")


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


def collect(context: CollectionContext, adapters: Mapping[str, SourceAdapter]) -> CollectionResult:
    if context.configured_catalog_version != CATALOG_VERSION:
        raise ValueError("Unknown collection catalog.")
    if not 1 <= context.attempt_number <= context.max_attempts <= 3:
        raise ValueError("Invalid trusted collection attempt budget.")
    for key, adapter in adapters.items():
        if key not in SOURCES or REGISTERED_ADAPTERS.get(key) is not adapter:
            raise ValueError("Unknown collection adapter binding.")
    deadline = time.monotonic() + 90
    results = []
    # Sequential reads intentionally use less than the four-read maximum. There
    # are no thread-based pseudo-timeouts or unbounded background provider calls.
    for key in SOURCES:
        adapter = adapters.get(key)
        if adapter is None:
            results.append(_unavailable(key, "source_not_configured"))
            continue
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise SourceTimeout()
            result = adapter(context, min(5.0, remaining))
            if result.source_key != key or result.payload_kind != SOURCES[key].payload_kind:
                raise ValueError("Adapter returned a mismatched source result.")
            results.append(result)
        except (TransientSourceError, TimeoutError, ConnectionError) as exc:
            if context.attempt_number < context.max_attempts:
                raise
            reason = (
                "timeout"
                if isinstance(exc, (SourceTimeout, TimeoutError))
                else "source_unavailable"
            )
            results.append(_unavailable(key, reason))
    return CollectionResult(context.workload_uid, tuple(results))


class CollectionHandler:
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

    def publish(self, db: Session, subject_id: UUID, result: CollectionResult):
        from cloudops.features.investigations.storage import store_batch

        run = db.get(InvestigationRun, subject_id)
        if run.state == "cancelled":
            return
        store_batch(db, subject_id, result.workload_uid, result.source_results)
        run.state = "insufficient_evidence"
        run.outcome_code = "analysis_unavailable"
        run.reason = "Evidence collection saved. Analysis and recovery planning are unavailable."
        run.finished_at = database_now(db)
