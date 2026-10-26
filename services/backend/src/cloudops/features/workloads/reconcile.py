"""Revision-fenced scope reconciliation; cluster reads never hold database locks."""

import os
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cloudops.features.workloads.models import ScopeState, ScopeTarget, Workload
from cloudops.features.workloads.scope_config import LiveScope, ScopeReader, load_scope
from cloudops.shared.config import settings
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError


@dataclass(frozen=True)
class SyncResult:
    current: int
    missing: int = 0
    unavailable: int = 0
    removed: int = 0


def conflict(message):
    raise DomainError(409, "scope_conflict", message)


def lock_state(db):
    db.execute(
        insert(ScopeState)
        .values(id=1, latest_sequence=0, applied_sequence=0, mode="legacy")
        .on_conflict_do_nothing()
    )
    return db.scalar(
        select(ScopeState)
        .where(ScopeState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def validate_binding(db, state, scope, previous):
    if not isinstance(scope, LiveScope):
        if state.mode == "v2":
            conflict("A live v2 policy cannot silently revert to legacy authority.")
        if previous:
            conflict("Cluster rotation requires a live v2 policy.")
        return
    cluster = scope.cluster
    if state.cluster_key and state.cluster_key != cluster.key:
        conflict("Cluster key changes require a reviewed identity migration.")
    if state.fingerprint and state.fingerprint != cluster.expected_fingerprint:
        if previous != state.fingerprint:
            conflict(
                "Use the exact previous fingerprint for an explicitly reviewed cluster rotation."
            )
    elif previous:
        conflict("The supplied previous fingerprint does not describe a pending rotation.")
    # A renamed cluster key must not fabricate a new identity for an old live target.
    live = db.scalars(select(Workload).where(Workload.source == "live_kubernetes")).all()
    desired = {(target.namespace, target.name) for target in scope.deployments}
    if any(
        row.cluster != cluster.key and (row.active or (row.namespace, row.name) in desired)
        for row in live
    ):
        conflict("Existing live identities use a different cluster key; preserve their binding.")


def sync(db, previous_fingerprint=None):
    path = settings().workloads_file
    scope, digest = load_scope(path)
    reader = None
    if isinstance(scope, LiveScope):
        reader = ScopeReader(scope, Path(os.environ.get("KUBE_READ_CONFIG", "")))
    with db.begin():
        state = lock_state(db)
        validate_binding(db, state, scope, previous_fingerprint)
        state.latest_sequence += 1
        sequence = state.latest_sequence
    # The global 60-second budget bounds sequential resolution at 100 targets.
    deadline = time.monotonic() + 60
    results = (
        [(target, reader.lookup(target, deadline)) for target in scope.deployments]
        if reader
        else []
    )
    with db.begin():
        state = lock_state(db)
        if state.latest_sequence != sequence:
            conflict("This synchronization was superseded; run a fresh explicit sync.")
        if load_scope(path)[1] != digest:
            conflict("Scope configuration changed during reads; run a fresh explicit sync.")
        validate_binding(db, state, scope, previous_fingerprint)
        if reader:
            result = publish_live(db, state, scope, results, previous_fingerprint)
            state.mode = "v2"
            state.cluster_key = scope.cluster.key
            state.fingerprint = scope.cluster.expected_fingerprint
        else:
            result = publish_legacy(db, scope)
        state.config_digest = digest
        state.applied_sequence = sequence
        return result


def retire(row, reason):
    row.active = False
    row.retirement_reason = reason


def publish_legacy(db, entries):
    rows = db.scalars(
        select(Workload)
        .order_by(Workload.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    identities = {(r.cluster, r.namespace, r.name, r.uid): r for r in rows}
    desired_names = {(e.cluster, e.namespace, e.name) for e in entries}
    for row in rows:
        if row.active:
            retire(
                row,
                "replaced"
                if (row.cluster, row.namespace, row.name) in desired_names
                else "removed",
            )
    for entry in entries:
        key = (entry.cluster, entry.namespace, entry.name, entry.uid)
        row = identities.get(key)
        if row:
            row.active, row.source, row.retirement_reason = True, entry.source, None
        else:
            db.add(Workload(**entry.model_dump()))
    return SyncResult(current=len(entries))


def publish_live(db, state, scope, results, rotating):
    now = database_now(db)
    targets = db.scalars(select(ScopeTarget).order_by(ScopeTarget.id).with_for_update()).all()
    rows = db.scalars(
        select(Workload)
        .order_by(Workload.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_name = {(t.namespace, t.name): t for t in targets if t.cluster == scope.cluster.key}
    by_id = {r.id: r for r in rows}
    desired = {(t.namespace, t.name) for t, _ in results}
    if rotating:
        previous_names = {key for key, target in by_name.items() if target.configured}
        if not previous_names:
            conflict("Rotation requires bound identities to establish cluster continuity.")
        if desired != previous_names or any(
            lookup.state != "present"
            or not by_name.get((t.namespace, t.name))
            or not by_name[(t.namespace, t.name)].workload_id
            or by_id[by_name[(t.namespace, t.name)].workload_id].uid != lookup.uid
            for t, lookup in results
        ):
            conflict(
                "Rotation requires the same configured scope and successfully verified current UIDs."
            )
    removed = 0
    for row in rows:
        if row.active and (
            row.cluster != scope.cluster.key or (row.namespace, row.name) not in desired
        ):
            retire(row, "removed")
            removed += 1
    for target in targets:
        if target.configured and (
            target.cluster != scope.cluster.key or (target.namespace, target.name) not in desired
        ):
            target.configured, target.state, target.reason = False, "removed", None
            target.last_attempt_at = now
    counts = {"present": 0, "missing": 0, "unavailable": 0}
    for item, lookup in results:
        key = (item.namespace, item.name)
        target = by_name.get(key)
        if target is None:
            target = ScopeTarget(
                cluster=scope.cluster.key,
                namespace=item.namespace,
                name=item.name,
                state="unavailable",
                last_attempt_at=now,
            )
            db.add(target)
        # Legacy UID-pinned migration can retain an existing identity even on a failed read.
        matching = [
            r for r in rows if (r.cluster, r.namespace, r.name) == (scope.cluster.key, *key)
        ]
        active = [r for r in matching if r.active]
        if len(active) > 1:
            conflict("A live target has conflicting active UID-pinned identities.")
        if target.workload_id is None and active:
            target.workload_id = active[0].id
        counts[lookup.state] += 1
        target.configured, target.last_attempt_at = True, now
        target.state = "current" if lookup.state == "present" else lookup.state
        target.reason = lookup.reason
        if lookup.state == "unavailable":
            continue
        target.last_success_at = now
        for old in active:
            if lookup.state == "missing" or old.uid != lookup.uid:
                retire(old, "deleted" if lookup.state == "missing" else "replaced")
        if lookup.state == "missing":
            continue
        row = next((r for r in matching if r.uid == lookup.uid), None)
        if row is None:
            row = Workload(
                cluster=scope.cluster.key,
                namespace=item.namespace,
                name=item.name,
                uid=lookup.uid,
                source="live_kubernetes",
            )
            db.add(row)
            db.flush()
            rows.append(row)
        row.active, row.retirement_reason, row.source = True, None, "live_kubernetes"
        target.workload_id = row.id
    return SyncResult(counts["present"], counts["missing"], counts["unavailable"], removed)
