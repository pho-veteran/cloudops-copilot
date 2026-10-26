"""Feature-owned observation scheduling, leases, publication and bounded history."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cloudops.features.workloads.models import (
    Workload,
    WorkloadObservation,
    WorkloadObservationAttempt,
    WorkloadObservationState,
)
from cloudops.features.workloads.schemas import (
    ObservationRefreshView,
    WorkloadObservationHistoryItem,
    WorkloadObservationPage,
    WorkloadObservationPayload,
    WorkloadObservationView,
)
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError

OBSERVATION_INTERVAL_SECONDS = 30
OBSERVATION_STALE_SECONDS = 60
OBSERVATION_LEASE_SECONDS = 45
HISTORY_LIMIT = 100
HISTORY_DAYS = 7

# Only these sanitized reason codes may reach persisted or public state.
FAILURE_REASONS = frozenset(
    {
        "cluster_unavailable",
        "target_changed",
        "observation_failed",
        "timeout",
        "invalid_payload",
        "workload_unavailable",
    }
)


@dataclass(frozen=True)
class ObservationClaim:
    """Detached lease identity; no database transaction survives a cluster read."""

    workload_id: UUID
    workload_uid: str
    cluster: str
    namespace: str
    name: str
    lease_token: UUID


def request_refresh(db: Session, workload_id: UUID, actor_id: UUID) -> ObservationRefreshView:
    with db.begin():
        workload = db.scalar(select(Workload).where(Workload.id == workload_id).with_for_update())
        if not workload:
            raise DomainError(404, "workload_not_found", "Workload not found.")
        if not workload.active:
            raise DomainError(422, "workload_unavailable", "Choose an active configured workload.")
        if workload.source != "live_kubernetes":
            raise DomainError(
                409,
                "observation_unavailable",
                "Synthetic workloads have no live observation source.",
            )
        now = database_now(db)
        state = db.scalar(
            select(WorkloadObservationState)
            .where(WorkloadObservationState.workload_id == workload_id)
            .with_for_update()
        )
        if not state:
            state = WorkloadObservationState(workload_id=workload_id, due_at=now)
            db.add(state)
            db.flush()
        state.requested_at = now
        state.requested_by = actor_id
        # One outstanding lease is enough; a request never stacks work.
        if state.lease_token is None:
            state.due_at = now
        return ObservationRefreshView(workload_id=workload_id, requested_at=now)


def claim_due(db: Session, now: datetime, limit: int) -> list[ObservationClaim]:
    with db.begin():
        db.execute(
            insert(WorkloadObservationState)
            .from_select(
                ["workload_id", "due_at"],
                select(Workload.id, literal(now)).where(
                    Workload.active, Workload.source == "live_kubernetes"
                ),
            )
            .on_conflict_do_nothing(index_elements=["workload_id"])
        )
        rows = db.execute(
            select(WorkloadObservationState, Workload)
            .join(Workload, Workload.id == WorkloadObservationState.workload_id)
            .where(
                Workload.active,
                Workload.source == "live_kubernetes",
                WorkloadObservationState.due_at <= now,
                or_(
                    WorkloadObservationState.lease_until.is_(None),
                    WorkloadObservationState.lease_until <= now,
                ),
            )
            .order_by(WorkloadObservationState.due_at, WorkloadObservationState.workload_id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=WorkloadObservationState)
        ).all()
        claims = []
        for state, workload in rows:
            token = uuid4()
            state.lease_token = token
            state.lease_until = now + timedelta(seconds=OBSERVATION_LEASE_SECONDS)
            state.due_at = now + timedelta(seconds=OBSERVATION_INTERVAL_SECONDS)
            state.requested_at = None
            claims.append(
                ObservationClaim(
                    workload_id=workload.id,
                    workload_uid=workload.uid,
                    cluster=workload.cluster,
                    namespace=workload.namespace,
                    name=workload.name,
                    lease_token=token,
                )
            )
        return claims


def publish_success(
    db: Session, claim: ObservationClaim, payload: WorkloadObservationPayload
) -> bool:
    with db.begin():
        state = locked_state(db, claim)
        if state is None:
            return False
        if payload.deployment_uid != claim.workload_uid:
            # A replaced target must never be published under the retired identity.
            return False
        now = database_now(db)
        db.add(
            WorkloadObservation(
                workload_id=claim.workload_id,
                deployment_uid=payload.deployment_uid,
                status=payload.status,
                payload=payload.model_dump(mode="json"),
                observed_at=now,
            )
        )
        state.last_success_at = now
        record_attempt(state, now, "succeeded", None)
        release(state)
        db.flush()
        prune_history(db, claim.workload_id, now)
        return True


def publish_failure(db: Session, claim: ObservationClaim, reason_code: str) -> bool:
    with db.begin():
        state = locked_state(db, claim)
        if state is None:
            return False
        now = database_now(db)
        reason = reason_code if reason_code in FAILURE_REASONS else "observation_failed"
        db.add(
            WorkloadObservationAttempt(
                workload_id=claim.workload_id,
                state="failed",
                reason=reason,
                attempted_at=now,
            )
        )
        record_attempt(state, now, "failed", reason)
        release(state)
        db.flush()
        prune_history(db, claim.workload_id, now)
        return True


def locked_state(db: Session, claim: ObservationClaim) -> WorkloadObservationState | None:
    workload = db.scalar(select(Workload).where(Workload.id == claim.workload_id).with_for_update())
    if not workload or not workload.active or workload.uid != claim.workload_uid:
        return None
    state = db.scalar(
        select(WorkloadObservationState)
        .where(WorkloadObservationState.workload_id == claim.workload_id)
        .with_for_update()
    )
    now = database_now(db)
    if (
        not state
        or state.lease_token != claim.lease_token
        or state.lease_until is None
        or state.lease_until < now
    ):
        return None
    return state


def record_attempt(state, now, result, reason):
    state.last_attempt_at = now
    state.last_attempt_state = result
    state.last_attempt_reason = reason


def release(state):
    state.lease_token = None
    state.lease_until = None
    state.requested_at = None


def prune_history(db: Session, workload_id: UUID, now: datetime):
    cutoff = now - timedelta(days=HISTORY_DAYS)
    observations = (
        select(WorkloadObservation.id)
        .where(WorkloadObservation.workload_id == workload_id)
        .order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc())
        .limit(HISTORY_LIMIT)
        .scalar_subquery()
    )
    db.execute(
        delete(WorkloadObservation).where(
            WorkloadObservation.workload_id == workload_id,
            or_(
                WorkloadObservation.observed_at < cutoff,
                WorkloadObservation.id.notin_(observations),
            ),
        )
    )
    attempts = (
        select(WorkloadObservationAttempt.id)
        .where(WorkloadObservationAttempt.workload_id == workload_id)
        .order_by(
            WorkloadObservationAttempt.attempted_at.desc(),
            WorkloadObservationAttempt.id.desc(),
        )
        .limit(HISTORY_LIMIT)
        .scalar_subquery()
    )
    db.execute(
        delete(WorkloadObservationAttempt).where(
            WorkloadObservationAttempt.workload_id == workload_id,
            or_(
                WorkloadObservationAttempt.attempted_at < cutoff,
                WorkloadObservationAttempt.id.notin_(attempts),
            ),
        )
    )


def latest_payload(db: Session, workload_id: UUID) -> dict | None:
    """Read the prior successful payload for restart-delta comparison."""
    row = db.scalar(
        select(WorkloadObservation)
        .where(WorkloadObservation.workload_id == workload_id)
        .order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc())
        .limit(1)
    )
    return row.payload if row else None


def read_observation(db: Session, workload_id: UUID, now: datetime) -> WorkloadObservationView:
    workload = db.get(Workload, workload_id)
    if not workload:
        raise DomainError(404, "workload_not_found", "Workload not found.")
    state = db.get(WorkloadObservationState, workload_id)
    latest = db.scalar(
        select(WorkloadObservation)
        .where(WorkloadObservation.workload_id == workload_id)
        .order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc())
        .limit(1)
    )
    freshness = "unavailable"
    if latest:
        freshness = (
            "fresh"
            if now - latest.observed_at <= timedelta(seconds=OBSERVATION_STALE_SECONDS)
            else "stale"
        )
    return WorkloadObservationView(
        workload_id=workload.id,
        workload_active=workload.active,
        source=workload.source,
        availability="available" if latest else "unavailable",
        freshness=freshness,
        observation=WorkloadObservationPayload.model_validate(latest.payload) if latest else None,
        observed_at=latest.observed_at if latest else None,
        last_attempt_at=state.last_attempt_at if state else None,
        last_attempt_state=state.last_attempt_state if state else None,
        last_attempt_reason=state.last_attempt_reason if state else None,
        pending_refresh=bool(state and state.requested_at),
    )


def list_observations(
    db: Session, workload_id: UUID, limit: int, offset: int
) -> WorkloadObservationPage:
    if not db.get(Workload, workload_id):
        raise DomainError(404, "workload_not_found", "Workload not found.")
    rows = db.scalars(
        select(WorkloadObservation)
        .where(WorkloadObservation.workload_id == workload_id)
        .order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return WorkloadObservationPage(
        items=[
            WorkloadObservationHistoryItem(
                id=row.id,
                workload_id=row.workload_id,
                deployment_uid=row.deployment_uid,
                status=row.status,
                observed_at=row.observed_at,
                payload=WorkloadObservationPayload.model_validate(row.payload),
            )
            for row in rows[:limit]
        ],
        next_offset=offset + limit if len(rows) > limit else None,
    )
