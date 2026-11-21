"""Short capture/publication transactions around cancellable external reads."""

import base64
import asyncio
import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, text, tuple_

from cloudops.features.workloads.models import ScopeState, Workload
from cloudops.features.workloads.observation_evaluator import ObservationError, compare
from cloudops.features.workloads.observation_models import (
    ObservationAttempt,
    ObservationState,
    WorkloadObservation,
)
from cloudops.features.workloads.observation_reader import ObservationReader, reader_bindings
from cloudops.features.workloads.observation_schemas import (
    Comparison,
    DirectOutcome,
    HistoryItem,
    Identity,
    Observation,
    ObservationDetail,
    ObservationPage,
    ObservationSummary,
    PublicObservation,
)
from cloudops.features.workloads.observation_settings import POLICY
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError

# Keep synchronous SQL off the HTTP cancellation loop. Connections and statements
# have independent bounds; cancellation cannot grant a late SQL claim authority.
_database_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="observation-db")


async def database_call(function, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(
        _database_executor, partial(function, *args, **kwargs)
    )


@dataclass(frozen=True)
class Claim:
    identity: Identity
    token: UUID
    sequence: int
    started_at: datetime
    scope_fingerprint: str | None = None
    public_config_id: str | None = None


def observation_binding(reader: str | None, scope_digest: str | None, epoch: int) -> str | None:
    if reader is None or scope_digest is None:
        return None
    return hashlib.sha256(f"{reader}:{scope_digest}:{epoch}".encode()).hexdigest()


def transaction_limits(db):
    db.execute(text("SET LOCAL lock_timeout = '1s'"))
    db.execute(text("SET LOCAL statement_timeout = '2s'"))


def claim(workload_id, purpose, factory=None, *, reader_identity=None, public_reader=None):
    if purpose not in {"scheduled", "baseline_candidate", "baseline_confirm", "recovery_context"}:
        raise ValueError("Unknown observation purpose")
    with (factory or sessions())() as db, db.begin():
        transaction_limits(db)
        # Distinct from incident admission. All processes share these finite slots.
        db.execute(text("SELECT pg_advisory_xact_lock(742027)"))
        workload = db.scalar(select(Workload).where(Workload.id == workload_id).with_for_update())
        if workload is None:
            raise ObservationError("target_changed")
        if not workload.active:
            raise ObservationError("inactive")
        if workload.source != "live_kubernetes":
            raise ObservationError("fixture")
        scope = db.get(ScopeState, 1)
        fingerprint = None
        if scope and scope.mode == "v2":
            if scope.cluster_key != workload.cluster or not scope.fingerprint:
                raise ObservationError("cluster_identity_mismatch")
            fingerprint = scope.fingerprint
        now = database_now(db)
        live = ObservationState.lease_until > now
        total = db.scalar(select(func.count()).select_from(ObservationState).where(live))
        scheduled = db.scalar(
            select(func.count())
            .select_from(ObservationState)
            .where(live, ObservationState.purpose == "scheduled")
        )
        if total >= POLICY.global_slots or (
            purpose == "scheduled" and scheduled >= POLICY.scheduled_slots
        ):
            return None
        state = db.get(ObservationState, workload_id, with_for_update=True)
        if state is None:
            state = ObservationState(
                workload_id=workload_id, next_due_at=now, sequence=0, failures=0
            )
            db.add(state)
        if state.lease_until and state.lease_until > now:
            return None
        if purpose == "scheduled" and state.next_due_at > now:
            return None
        if state.lease_token:
            prior = db.get(ObservationAttempt, state.lease_token)
            if prior and prior.outcome == "running":
                prior.outcome, prior.reason, prior.finished_at = "expired", "lease_expired", now
        token = uuid4()
        state.sequence += 1
        state.lease_token, state.purpose = token, purpose
        state.lease_until = now + timedelta(seconds=POLICY.lease_seconds)
        state.deadline_at = now + timedelta(seconds=POLICY.deadline_seconds)
        state.last_attempt_at, state.latest_outcome = now, "running"
        digest = scope.config_digest if scope else None
        state.attempt_binding = observation_binding(
            reader_identity, digest, workload.observation_epoch
        )
        state.bound_attempt_sequence = state.sequence
        config_id = observation_binding(public_reader, digest, workload.observation_epoch)
        state.next_due_at = now + timedelta(seconds=POLICY.poll_seconds)
        state.partial_payload = None
        db.add(
            ObservationAttempt(
                id=token,
                workload_id=workload_id,
                sequence=state.sequence,
                started_at=now,
                outcome="running",
            )
        )
        return Claim(
            Identity(
                workload_id=workload.id,
                cluster=workload.cluster,
                namespace=workload.namespace,
                name=workload.name,
                uid=workload.uid,
                epoch=workload.observation_epoch,
            ),
            token,
            state.sequence,
            now,
            fingerprint,
            config_id,
        )


def cleanup(db, workload_id, now):
    cutoff = now - timedelta(days=POLICY.history_days)
    for model, time_column in (
        (WorkloadObservation, WorkloadObservation.observed_at),
        (ObservationAttempt, ObservationAttempt.started_at),
    ):
        retained = (
            select(model.id)
            .where(model.workload_id == workload_id)
            .order_by(time_column.desc(), model.id.desc())
            .limit(POLICY.history_count)
        )
        criteria = [
            model.workload_id == workload_id,
            or_(time_column < cutoff, model.id.not_in(retained)),
        ]
        if model is ObservationAttempt:
            criteria.append(model.outcome != "running")
        db.execute(delete(model).where(*criteria).execution_options(synchronize_session=False))


def finalize(captured, observation=None, reason=None, factory=None):
    with (factory or sessions())() as db, db.begin():
        transaction_limits(db)
        workload = db.scalar(
            select(Workload).where(Workload.id == captured.identity.workload_id).with_for_update()
        )
        state = db.get(ObservationState, captured.identity.workload_id, with_for_update=True)
        now = database_now(db)
        if not state or state.lease_token != captured.token or state.sequence != captured.sequence:
            return DirectOutcome(outcome="superseded", reason="superseded")
        attempt = db.get(ObservationAttempt, captured.token)
        identity_valid = (
            workload
            and workload.active
            and workload.source == "live_kubernetes"
            and workload.uid == captured.identity.uid
            and (workload.cluster, workload.namespace, workload.name)
            == (captured.identity.cluster, captured.identity.namespace, captured.identity.name)
            and workload.observation_epoch == captured.identity.epoch
        )
        if not identity_valid or state.lease_until <= now:
            attempt.outcome, attempt.reason, attempt.finished_at = "superseded", "superseded", now
            state.latest_outcome, state.last_failure_at, state.last_failure_reason = (
                "superseded",
                now,
                "superseded",
            )
            state.failure_binding = state.attempt_binding
            state.bound_failure_at = state.last_failure_at
            state.lease_token, state.lease_until = None, None
            cleanup(db, captured.identity.workload_id, now)
            return DirectOutcome(outcome="superseded", reason="superseded")
        if observation is not None:
            # Validate even a custom/internal reader's output before publication.
            observation = Observation.model_validate(observation.model_dump())
            if (
                observation.identity != captured.identity
                or observation.started_at != captured.started_at
            ):
                observation, reason = None, "target_changed"
            elif (
                observation.observed_at < captured.started_at
                or observation.observed_at > now + timedelta(seconds=1)
                or observation.observed_at > state.deadline_at
            ):
                observation, reason = None, "attempt_timeout"
            elif len(observation.model_dump_json().encode()) > POLICY.observation_bytes:
                observation, reason = None, "payload_limit"
        if now > state.deadline_at:
            observation, reason = None, "attempt_timeout"
        if observation is not None and observation.coverage == "complete":
            previous = (
                db.get(WorkloadObservation, state.last_success_id)
                if state.last_success_id
                else None
            )
            comparison = compare(
                Observation.model_validate(previous.payload) if previous else None, observation
            )
            db.add(
                WorkloadObservation(
                    id=observation.observation_id,
                    workload_id=workload.id,
                    epoch=workload.observation_epoch,
                    observed_at=observation.observed_at,
                    payload=observation.model_dump(mode="json"),
                    comparison=comparison.model_dump(mode="json"),
                )
            )
            state.last_success_at, state.last_success_id = (
                observation.observed_at,
                observation.observation_id,
            )
            state.latest_outcome, state.failures, state.partial_payload = "complete", 0, None
            state.success_sequence = captured.sequence
            state.success_binding = state.attempt_binding
            state.bound_success_id = state.last_success_id
            state.bound_success_config_id = captured.public_config_id
            attempt.outcome = "complete"
            state.next_due_at = now + timedelta(seconds=POLICY.poll_seconds)
        else:
            state.failures = min(state.failures + 1, 3)
            state.latest_outcome = "partial" if observation else "failed"
            reason = reason or (
                observation.reasons[0]
                if observation and observation.reasons
                else "invalid_response"
            )
            state.last_failure_at, state.last_failure_reason = now, reason
            state.failure_binding = state.attempt_binding
            state.bound_failure_at = state.last_failure_at
            state.partial_payload = observation.model_dump(mode="json") if observation else None
            state.next_due_at = now + timedelta(
                seconds=min(120, 30 * 2 ** (state.failures - 1)) + random.uniform(0, 3)
            )
            attempt.outcome, attempt.reason = state.latest_outcome, reason
        attempt.finished_at = now
        state.lease_token, state.lease_until = None, None
        db.flush()
        cleanup(db, workload.id, now)
        return DirectOutcome(outcome=state.latest_outcome, reason=reason, observation=observation)


async def observe_direct(workload_id, purpose="baseline_candidate", *, factory=None, reader=None):
    captured_binding, public_reader = reader_bindings() if reader is None else (None, None)
    try:
        captured = await database_call(
            claim,
            workload_id,
            purpose,
            factory,
            reader_identity=captured_binding,
            public_reader=public_reader,
        )
    except ObservationError as error:
        return DirectOutcome(outcome="failed", reason=error.code)
    if captured is None:
        return DirectOutcome(outcome="busy", reason="budget_exhausted")
    try:
        observation = await (
            reader
            or ObservationReader(
                expected_binding=captured_binding,
                expected_fingerprint=captured.scope_fingerprint,
            )
        ).read(captured.identity, captured.started_at)
        if captured_binding and reader_bindings()[0] != captured_binding:
            return await database_call(
                finalize, captured, reason="cluster_identity_mismatch", factory=factory
            )
        return await database_call(finalize, captured, observation, factory=factory)
    except ObservationError as error:
        return await database_call(finalize, captured, reason=error.code, factory=factory)


def summary(workload, state, observation, now):
    result = ObservationSummary(evaluated_at=now)
    if state:
        result.last_attempt_at, result.last_success_at = (
            state.last_attempt_at,
            state.last_success_at,
        )
        result.last_failure_at, result.last_failure_reason = (
            state.last_failure_at,
            state.last_failure_reason,
        )
        result.latest_outcome = state.latest_outcome
        result.poll_lag_seconds = max(0, (now - state.next_due_at).total_seconds())
        if state.last_success_at:
            result.age_seconds = max(0, (now - state.last_success_at).total_seconds())
            result.freshness = "stale" if result.age_seconds > POLICY.stale_seconds else "fresh"
            if state.last_success_at > now + timedelta(seconds=1):
                result.freshness = "unknown"
        result.refreshing = (
            state.latest_outcome == "running"
            and state.deadline_at is not None
            and state.deadline_at > now
        )
        if result.refreshing:
            result.refresh_remaining_seconds = max(0, (state.deadline_at - now).total_seconds())
    if workload.source != "live_kubernetes":
        result.reasons = ["fixture"]
    elif not workload.active:
        result.reasons = ["inactive"]
    elif state is None or state.last_attempt_at is None:
        result.reasons = ["not_observed"]
    elif state.latest_outcome == "running" and not result.refreshing:
        result.reasons = ["lease_expired"]
    elif state.latest_outcome == "running" and state.success_sequence != state.sequence - 1:
        result.reasons = [state.last_failure_reason or "not_observed"]
    elif state.latest_outcome not in {"complete", "running"}:
        result.reasons = [state.last_failure_reason or "cluster_unavailable"]
        result.coverage = "partial" if state.latest_outcome == "partial" else "unknown"
        if state.latest_outcome == "partial" and state.partial_payload:
            partial_reasons = Observation.model_validate(state.partial_payload).reasons
            result.reasons = list(dict.fromkeys([*result.reasons, *partial_reasons]))[:4]
    elif result.freshness != "fresh":
        result.reasons = ["stale" if result.freshness == "stale" else "not_observed"]
    elif not observation:
        result.reasons = ["history_expired"]
    elif observation.epoch != workload.observation_epoch:
        result.reasons = ["superseded"]
    else:
        value = Observation.model_validate(observation.payload)
        result.effective_health, result.coverage, result.reasons = (
            value.health,
            value.coverage,
            value.reasons,
        )
        result.desired, result.ready = value.deployment.desired, value.deployment.ready
    if (
        result.poll_lag_seconds
        and result.poll_lag_seconds > POLICY.poll_seconds
        and "budget_exhausted" not in result.reasons
    ):
        result.reasons = [*result.reasons, "budget_exhausted"][:16]
    return result


def summaries(db, workloads):
    ids = [w.id for w in workloads]
    states = {
        s.workload_id: s
        for s in db.scalars(select(ObservationState).where(ObservationState.workload_id.in_(ids)))
    }
    now = database_now(db)
    records = {
        o.id: o
        for o in db.scalars(
            select(WorkloadObservation).where(
                WorkloadObservation.observed_at >= now - timedelta(days=POLICY.history_days),
                WorkloadObservation.id.in_(
                    [s.last_success_id for s in states.values() if s.last_success_id]
                ),
            )
        )
    }
    return {
        w.id: summary(
            w,
            states.get(w.id),
            records.get(states[w.id].last_success_id) if w.id in states else None,
            now,
        )
        for w in workloads
    }


def require_identity(db, workload_id):
    workload = db.get(Workload, workload_id)
    if workload is None:
        raise DomainError(404, "workload_not_found", "Workload not found.")
    return workload


def public(value):
    return PublicObservation.model_validate(
        value.model_dump(
            exclude={
                "schema_version",
                "replicaset_resource_version",
                "pod_resource_version",
                "template_fields",
                "pod_template_digest",
                "config_references",
            }
        )
    )


def detail(db, workload_id):
    workload = require_identity(db, workload_id)
    state = db.get(ObservationState, workload_id)
    now = database_now(db)
    latest = (
        db.get(WorkloadObservation, state.last_success_id)
        if state and state.last_success_id
        else None
    )
    if latest and latest.observed_at < now - timedelta(days=POLICY.history_days):
        latest = None
    projected = summary(workload, state, latest, now)
    value = Observation.model_validate(latest.payload) if latest else None
    current = public(value) if value and projected.effective_health != "unknown" else None
    partial = (
        Observation.model_validate(state.partial_payload)
        if state
        and state.partial_payload
        and state.last_failure_at >= now - timedelta(days=POLICY.history_days)
        else None
    )
    if partial and workload.active and partial.identity.epoch == workload.observation_epoch:
        current = public(partial)
    historical = (
        public(value)
        if value and (current is None or current.observation_id != value.observation_id)
        else None
    )
    return ObservationDetail(
        summary=projected,
        current=current,
        historical=historical,
        comparison=Comparison.model_validate(latest.comparison) if latest else Comparison(),
    )


def history(db, workload_id, limit, cursor):
    require_identity(db, workload_id)
    query = select(WorkloadObservation).where(
        WorkloadObservation.workload_id == workload_id,
        WorkloadObservation.observed_at >= database_now(db) - timedelta(days=POLICY.history_days),
    )
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(data, list) or len(data) != 2:
                raise ValueError
            moment, identity = datetime.fromisoformat(data[0]), UUID(data[1])
            if moment.tzinfo is None or len(data) != 2:
                raise ValueError
        except (ValueError, TypeError, IndexError):
            raise DomainError(
                422, "invalid_cursor", "Use the cursor returned by this workload history."
            ) from None
        query = query.where(
            tuple_(WorkloadObservation.observed_at, WorkloadObservation.id) < (moment, identity)
        )
    rows = db.scalars(
        query.order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc()).limit(
            limit + 1
        )
    ).all()
    result = []
    for row in rows[:limit]:
        value = Observation.model_validate(row.payload)
        result.append(
            HistoryItem(
                observation_id=row.id,
                observed_at=row.observed_at,
                generation=value.deployment.generation,
                health=value.health,
                coverage=value.coverage,
            )
        )
    token = (
        base64.urlsafe_b64encode(
            json.dumps([rows[limit - 1].observed_at.isoformat(), str(rows[limit - 1].id)]).encode()
        )
        .decode()
        .rstrip("=")
        if len(rows) > limit
        else None
    )
    return ObservationPage(items=result, next_cursor=token)


def due(factory=None):
    with (factory or sessions())() as db, db.begin():
        transaction_limits(db)
        # Initialization shares claim's admission lock: no competing first insert.
        db.execute(text("SELECT pg_advisory_xact_lock(742027)"))
        now = database_now(db)
        # Initialization and cleanup are bounded to the configured scope.
        active = db.scalars(
            select(Workload)
            .where(Workload.active, Workload.source == "live_kubernetes")
            .order_by(Workload.id)
            .limit(100)
        ).all()
        states = set(
            db.scalars(
                select(ObservationState.workload_id).where(
                    ObservationState.workload_id.in_([w.id for w in active])
                )
            )
        )
        for workload in active:
            if workload.id not in states:
                from sqlalchemy.dialects.postgresql import insert

                db.execute(
                    insert(ObservationState)
                    .values(
                        workload_id=workload.id,
                        next_due_at=now + timedelta(seconds=workload.id.int % 30),
                        sequence=0,
                        failures=0,
                    )
                    .on_conflict_do_nothing()
                )
        db.flush()
        return list(
            db.scalars(
                select(ObservationState.workload_id)
                .join(Workload)
                .where(
                    Workload.active,
                    Workload.source == "live_kubernetes",
                    ObservationState.next_due_at <= now,
                    or_(
                        ObservationState.lease_until.is_(None), ObservationState.lease_until <= now
                    ),
                )
                .order_by(ObservationState.next_due_at, ObservationState.workload_id)
                .limit(POLICY.scheduled_slots)
            )
        )


def sweep(factory=None):
    with (factory or sessions())() as db, db.begin():
        transaction_limits(db)
        now = database_now(db)
        # Old inactive histories also expire; one identity per sweep bounds lock/SQL work.
        oldest = db.scalar(
            select(WorkloadObservation.workload_id)
            .where(WorkloadObservation.observed_at < now - timedelta(days=POLICY.history_days))
            .limit(1)
        )
        failed = db.scalar(
            select(ObservationAttempt.workload_id)
            .where(ObservationAttempt.started_at < now - timedelta(days=POLICY.history_days))
            .limit(1)
        )
        for workload_id in {oldest, failed} - {None}:
            cleanup(db, workload_id, now)
        partials = db.scalars(
            select(ObservationState)
            .where(
                ObservationState.partial_payload.is_not(None),
                ObservationState.last_failure_at < now - timedelta(days=POLICY.history_days),
            )
            .order_by(ObservationState.workload_id)
            .limit(10)
            .with_for_update(skip_locked=True)
        ).all()
        for state in partials:
            state.partial_payload = None
        # A dead process has no authority after its lease, even without a new attempt.
        expired = db.scalars(
            select(ObservationState)
            .where(ObservationState.lease_until < now)
            .order_by(ObservationState.workload_id)
            .limit(10)
            .with_for_update(skip_locked=True)
        ).all()
        for state in expired:
            attempt = db.get(ObservationAttempt, state.lease_token)
            if attempt and attempt.outcome == "running":
                attempt.outcome, attempt.reason, attempt.finished_at = (
                    "expired",
                    "lease_expired",
                    now,
                )
            state.latest_outcome, state.last_failure_at, state.last_failure_reason = (
                "expired",
                now,
                "lease_expired",
            )
            state.lease_token, state.lease_until, state.partial_payload = None, None, None
            cleanup(db, state.workload_id, now)
