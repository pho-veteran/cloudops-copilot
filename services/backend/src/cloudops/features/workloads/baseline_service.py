"""Fresh observations outside SQL transactions, immutable confirmed provenance."""

import base64
import copy
from datetime import datetime, timedelta
import hashlib
import json
from uuid import UUID
from sqlalchemy import select, tuple_
from cloudops.features.access.service import lock_principal
from cloudops.features.workloads.baseline_schemas import (
    PROFILE,
    LEGACY_LIMITATIONS,
    LIMITATIONS,
    BaselineEnvelope,
    BaselinePage,
    BaselinePayload,
    BaselineView,
    CandidateView,
    Provenance,
)
from cloudops.features.workloads.models import Baseline, BaselineCandidate, Workload
from cloudops.features.workloads.observation_evaluator import compare
from cloudops.features.workloads.observation_service import (
    database_call,
    observe_direct,
    public,
    transaction_limits,
)
from cloudops.features.workloads.service import require_active
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError


def invalid(message="The candidate changed or expired. Inspect a fresh healthy candidate."):
    raise DomainError(409, "candidate_changed", message)


def envelope(observation):
    return BaselineEnvelope(
        identity={
            k: getattr(observation.identity, k) for k in ("cluster", "namespace", "name", "uid")
        },
        containers=[
            {
                "name": c.name,
                "image": c.image,
                "resources": {"requests": c.requests, "limits": c.limits},
            }
            for c in sorted(observation.template_fields, key=lambda v: v.name)
        ],
        config_references=sorted(
            observation.config_references, key=lambda v: (v.path, v.model_dump_json())
        ),
    )


def canonical(value):
    body = BaselineEnvelope.model_validate(
        value.model_dump(include=set(BaselineEnvelope.model_fields))
    )
    body.containers.sort(key=lambda item: item.name)
    body.config_references.sort(key=lambda item: (item.path, item.model_dump_json()))
    return json.dumps(
        body.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def payload(snapshot):
    return BaselinePayload.model_validate(
        {k: v for k, v in snapshot.items() if k != "_session_binding"}
    )


def baseline_view(row, now):
    return BaselineView(
        id=row.id,
        workload_id=row.workload_id,
        candidate_id=row.candidate_id,
        profile=PROFILE if row.snapshot.get("profile") == PROFILE else "legacy/v0",
        digest=row.digest,
        snapshot={k: copy.deepcopy(v) for k, v in row.snapshot.items() if k != "_session_binding"},
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        age_seconds=max(0, (now - row.confirmed_at).total_seconds()),
        limitations=LIMITATIONS if row.snapshot.get("profile") == PROFILE else LEGACY_LIMITATIONS,
    )


def healthy_result(result):
    if result.outcome == "busy":
        raise DomainError(
            409,
            "observation_busy",
            "Another bounded observation is running. Wait for it to finish, then inspect a fresh candidate; this attempt did not establish workload health.",
        )
    if result.outcome == "superseded":
        raise DomainError(
            409,
            "observation_superseded",
            "The observation was superseded. Inspect a fresh candidate for the current identity.",
        )
    if (
        result.outcome != "complete"
        or not result.observation
        or result.observation.health != "healthy"
        or result.observation.coverage != "complete"
    ):
        raise DomainError(
            409,
            "baseline_unavailable",
            "A fresh, complete healthy rollout is required. This read could not establish it. Inspect owned health and related incidents, then retry or investigate manually.",
        )
    return result.observation


def check_current(workload, observation, now):
    identity = observation.identity
    if (
        workload.source != "live_kubernetes"
        or workload.id != identity.workload_id
        or workload.observation_epoch != identity.epoch
        or any(
            getattr(workload, k) != getattr(identity, k)
            for k in ("cluster", "namespace", "name", "uid")
        )
        or not timedelta(seconds=-1) <= now - observation.observed_at <= timedelta(seconds=15)
    ):
        invalid()


def persist_candidate(workload_id, observation, binding):
    with sessions()() as db, db.begin():
        transaction_limits(db)
        lock_principal(db, binding)
        workload = require_active(db, workload_id)
        now = database_now(db)
        check_current(workload, observation, now)
        value = BaselinePayload(
            **envelope(observation).model_dump(), provenance=Provenance(first=public(observation))
        )
        snapshot = value.model_dump(mode="json", exclude_none=True)
        snapshot["_session_binding"] = binding
        row = BaselineCandidate(workload_id=workload_id, digest=digest(value), snapshot=snapshot)
        db.add(row)
        db.flush()
        return CandidateView(
            id=row.id,
            workload_id=row.workload_id,
            digest=row.digest,
            snapshot=value,
            created_at=row.created_at,
            confirm_after=row.created_at + timedelta(seconds=5),
            expires_at=observation.observed_at + timedelta(minutes=5),
            server_time=now,
        )


async def candidate(workload_id, binding):
    observation = healthy_result(await observe_direct(workload_id, "baseline_candidate"))
    return await database_call(persist_candidate, workload_id, observation, binding)


def checked_candidate(db, workload_id, body, binding):
    workload = require_active(db, workload_id)
    row = db.scalar(
        select(BaselineCandidate).where(BaselineCandidate.id == body.candidate_id).with_for_update()
    )
    if (
        not row
        or row.workload_id != workload_id
        or row.digest != body.digest
        or row.snapshot.get("_session_binding") != binding
    ):
        invalid()
    try:
        value = payload(row.snapshot)
    except (ValueError, TypeError):
        invalid()
    if digest(value) != row.digest:
        invalid()
    first_identity = value.provenance.first.identity
    if (
        first_identity.workload_id != workload.id
        or first_identity.epoch != workload.observation_epoch
        or any(
            getattr(first_identity, key) != getattr(workload, key)
            for key in ("cluster", "namespace", "name", "uid")
        )
    ):
        invalid()
    now = database_now(db)
    prior = db.scalar(select(Baseline).where(Baseline.candidate_id == row.id))
    if prior:
        return workload, row, value, baseline_view(prior, now)
    if now - row.created_at < timedelta(
        seconds=5
    ) or now - value.provenance.first.observed_at > timedelta(minutes=5):
        invalid(
            "Wait at least five seconds, then explicitly confirm within five minutes of the first read."
        )
    return workload, row, value, None


def prepare_confirmation(workload_id, body, binding):
    with sessions()() as db, db.begin():
        transaction_limits(db)
        lock_principal(db, binding, operator=True)
        return checked_candidate(db, workload_id, body, binding)[3]


def persist_confirmation(workload_id, body, binding, observation):
    with sessions()() as db, db.begin():
        transaction_limits(db)
        user = lock_principal(db, binding, operator=True)
        workload, row, value, prior = checked_candidate(db, workload_id, body, binding)
        if prior:
            return prior
        now = database_now(db)
        check_current(workload, observation, now)
        first = value.provenance.first
        if (
            first.identity != observation.identity
            or first.health != "healthy"
            or first.coverage != "complete"
            or first.deployment.generation != observation.deployment.generation
            or digest(envelope(observation)) != row.digest
            or not timedelta(seconds=5)
            <= observation.observed_at - first.observed_at
            <= timedelta(minutes=5)
        ):
            invalid()
        left = observation.model_copy(
            update={"pods": first.pods, "identity": first.identity, "coverage": first.coverage}
        )
        comparison = compare(left, observation)
        if comparison.state != "comparable" or comparison.restart_delta != 0:
            invalid("Pod/container coverage or restart counters changed. Review a fresh candidate.")
        value.provenance.second = public(observation)
        confirmed = Baseline(
            workload_id=workload_id,
            candidate_id=row.id,
            digest=row.digest,
            snapshot=value.model_dump(mode="json", exclude_none=True),
            confirmed_by=user.id,
        )
        db.add(confirmed)
        db.flush()
        return baseline_view(confirmed, now)


async def confirm(workload_id, body, binding):
    prior = await database_call(prepare_confirmation, workload_id, body, binding)
    if prior:
        return prior
    observation = healthy_result(await observe_direct(workload_id, "baseline_confirm"))
    return await database_call(persist_confirmation, workload_id, body, binding, observation)


def history(db, workload_id, limit, cursor):
    if not db.get(Workload, workload_id):
        raise DomainError(404, "workload_not_found", "Workload not found.")
    query = select(Baseline).where(Baseline.workload_id == workload_id)
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(data, list) or len(data) != 2:
                raise ValueError
            moment, identity = datetime.fromisoformat(data[0]), UUID(data[1])
            if moment.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, IndexError):
            raise DomainError(
                422, "invalid_cursor", "Use the cursor returned by baseline history."
            ) from None
        query = query.where(tuple_(Baseline.confirmed_at, Baseline.id) < (moment, identity))
    rows = db.scalars(
        query.order_by(Baseline.confirmed_at.desc(), Baseline.id.desc()).limit(limit + 1)
    ).all()
    now = database_now(db)
    token = None
    if len(rows) > limit:
        token = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [rows[limit - 1].confirmed_at.isoformat(), str(rows[limit - 1].id)]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return BaselinePage(items=[baseline_view(r, now) for r in rows[:limit]], next_cursor=token)
