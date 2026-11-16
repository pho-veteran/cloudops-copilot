"""Recovery-owned provenance inspection. This cannot admit an executable plan."""

from uuid import UUID
from fastapi import APIRouter, Depends, Request

from cloudops.features.access.dependencies import COOKIE, current_session
from cloudops.features.access.service import lock_principal, token_hash
from cloudops.features.workloads.baseline_schemas import PROFILE, RestoreContext
from cloudops.features.workloads.baseline_service import (
    baseline_view,
    check_current,
    digest,
    envelope,
    payload,
)
from cloudops.features.workloads.models import Baseline, Workload
from cloudops.features.workloads.observation_service import (
    database_call,
    observe_direct,
    transaction_limits,
)
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError, ERROR_RESPONSES

router = APIRouter(
    prefix="/api/workloads",
    tags=["recovery"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(current_session)],
)


def load(workload_id, baseline_id, binding, observation=None):
    with sessions()() as db, db.begin():
        transaction_limits(db)
        lock_principal(db, binding)
        workload = db.get(Workload, workload_id)
        if not workload:
            raise DomainError(404, "workload_not_found", "Workload not found.")
        if baseline_id is None:
            return RestoreContext(state="manual_only", reason="missing")
        row = db.get(Baseline, baseline_id)
        if not row:
            return RestoreContext(state="manual_only", reason="missing")
        if row.workload_id != workload_id:
            return RestoreContext(state="manual_only", reason="mismatch")
        view = baseline_view(row, database_now(db))
        if view.profile != PROFILE:
            return RestoreContext(state="manual_only", reason="legacy", baseline=view)
        try:
            value = payload(row.snapshot)
            valid = (
                digest(value) == row.digest
                and value.identity.uid == workload.uid
                and all(
                    getattr(value.identity, key) == getattr(workload, key)
                    for key in ("cluster", "namespace", "name")
                )
            )
            valid = valid and value.provenance.second is not None
        except (ValueError, TypeError):
            valid = False
        if not valid:
            return RestoreContext(state="manual_only", reason="mismatch", baseline=view)
        if not workload.active or workload.source != "live_kubernetes":
            return RestoreContext(
                state="manual_only",
                reason="mismatch" if workload.retirement_reason == "replaced" else "unavailable",
                baseline=view,
            )
        if observation is not None:
            if any(
                getattr(workload, key) != getattr(observation.identity, key)
                for key in ("cluster", "namespace", "name", "uid")
            ):
                return RestoreContext(state="manual_only", reason="mismatch", baseline=view)
            try:
                check_current(workload, observation, database_now(db))
            except DomainError:
                return RestoreContext(state="manual_only", reason="unavailable", baseline=view)
        return view


def supported_diff(saved, current):
    left = {c.name: c for c in saved.containers}
    right = {c.name: c for c in current.containers}
    if left.keys() != right.keys():
        return None
    changes = []
    for name in sorted(left):
        if left[name].image != right[name].image:
            changes.append(
                {
                    "path": f"containers.{name}.image",
                    "current": right[name].image,
                    "baseline": left[name].image,
                }
            )
        for group in ("requests", "limits"):
            for key in ("cpu", "memory"):
                old = getattr(getattr(left[name].resources, group), key)
                new = getattr(getattr(right[name].resources, group), key)
                if old != new:
                    changes.append(
                        {
                            "path": f"containers.{name}.resources.{group}.{key}",
                            "current": new,
                            "baseline": old,
                        }
                    )
    return changes


async def resolve_restore_baseline(workload_id, baseline_id, binding):
    selected = await database_call(load, workload_id, baseline_id, binding)
    if isinstance(selected, RestoreContext):
        return selected
    result = await observe_direct(workload_id, "recovery_context")
    # Re-read authentication, immutable selected record and identity after external I/O.
    selected = await database_call(load, workload_id, baseline_id, binding, result.observation)
    if isinstance(selected, RestoreContext):
        return selected
    if result.outcome != "complete" or result.observation is None:
        unsupported = (
            result.observation is not None and "unsupported_reference" in result.observation.reasons
        )
        return RestoreContext(
            state="manual_only",
            reason="mismatch"
            if result.reason == "target_changed"
            else "unsupported"
            if unsupported
            else "unavailable",
            baseline=selected,
        )
    current = envelope(result.observation)
    saved = payload(selected.snapshot)
    if saved.identity != current.identity:
        return RestoreContext(state="manual_only", reason="mismatch", baseline=selected)
    changes = supported_diff(saved, current)
    reason = (
        "unsupported"
        if changes is None
        else "config_unverified"
        if saved.config_references != current.config_references
        else "no_supported_diff"
        if not changes
        else "restore_not_implemented"
    )
    return RestoreContext(
        state="baseline_context" if reason == "restore_not_implemented" else "manual_only",
        reason=reason,
        baseline=selected,
        current_uid=current.identity.uid,
        supported_diff=changes,
    )


@router.get(
    "/{workload_id}/recovery-context",
    operation_id="get_recovery_context",
    response_model=RestoreContext,
)
async def recovery_context(workload_id: UUID, request: Request, baseline_id: UUID | None = None):
    return await resolve_restore_baseline(
        workload_id, baseline_id, token_hash(request.cookies[COOKIE])
    )
