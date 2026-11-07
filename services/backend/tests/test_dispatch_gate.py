"""Trusted DispatchPermit/v1 gates promotion, claims, checkpoints and publication for every kind."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import checkpoint_adapters as adapters
import pytest
from conftest import sign_in
from sqlalchemy import func, select, update
from test_access_admission import create, incident_body
from test_collection_checkpoints import bound, context_for  # noqa: F401
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.dispatch import CLASSES, GATE, PERMIT_CLASSES
from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.investigations.collector import collect_checkpointed
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.promotion import promote_deferred
from cloudops.features.recovery import service as recovery
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.shared import dispatch_permit as permits
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Claim, Job, JobAttempt, checkpoint, claim_next, finish

permission_context = _permission_context
ACTOR = {"uid": None, "channel": "test"}


def change(root, operation, **kwargs):
    return permits.transition(
        root,
        operation,
        CLASSES,
        reason="Supported restore drill",
        evidence=["test"],
        actor=ACTOR,
        **kwargs,
    )


def seed_jobs():
    with sessions()() as db, db.begin():
        for kind in PERMIT_CLASSES:
            db.add(Job(kind=kind, subject_id=uuid4()))


def snapshot():
    with sessions()() as db:
        return (
            sorted((j.kind, j.state, j.attempts) for j in db.scalars(select(Job))),
            db.scalar(select(func.count()).select_from(JobAttempt)),
        )


def write_document(root, **changes):
    path = root / "permit.json"
    document = {**json.loads(path.read_text()), **changes}
    path.write_text(json.dumps(document))


@pytest.mark.parametrize(
    "denial", ["permit_missing", "permit_invalid", "permit_stale", "recovery_mode", "class_denied"]
)
def test_every_job_kind_is_denied_without_a_valid_permit(
    client, dispatch_permit, monkeypatch, denial
):
    seed_jobs()
    before = snapshot()
    if denial == "permit_missing":
        monkeypatch.delenv("JOB_DISPATCH_ROOT")
    elif denial == "permit_invalid":
        (dispatch_permit / "permit.json").write_text("not json")
    elif denial == "permit_stale":
        write_document(
            dispatch_permit,
            valid_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
    elif denial == "recovery_mode":
        change(dispatch_permit, "enter_recovery")
    else:
        # Normal-looking file whose classes omit every current job kind.
        write_document(dispatch_permit, allowed_job_kinds=[], mode="normal")
    assert GATE.current()[2] == denial
    assert claim_next(HANDLERS, GATE) is None
    assert not run_once()
    assert snapshot() == before


def test_recovery_denies_promotion_until_collection_is_resumed(client, dispatch_permit):
    ids = create(client, sign_in(client), incident_body(client)).json()
    with sessions()() as db, db.begin():
        db.execute(
            update(InvestigationRun)
            .where(InvestigationRun.id == UUID(ids["run_id"]))
            .values(queue_reason="capacity")
        )
        db.execute(Job.__table__.delete().where(Job.subject_id == UUID(ids["run_id"])))
    change(dispatch_permit, "enter_recovery")
    assert promote_deferred(gate=GATE) == 0
    run = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert run["dispatch"] == {"status": "paused", "reason": "recovery_mode"}
    assert run["cancel"] == {"allowed": True, "reason": "queued"}
    change(dispatch_permit, "resume", resume="recommendation")
    assert promote_deferred(gate=GATE) == 0
    change(dispatch_permit, "resume", resume="collection")
    assert promote_deferred(gate=GATE) == 1
    assert run_once()


def test_generation_change_fences_an_admitted_attempt_permanently(
    client,
    bound,  # noqa: F811
    dispatch_permit,
):
    ids = create(client, sign_in(client), incident_body(client)).json()
    claim = claim_next(HANDLERS, GATE)
    context = context_for(claim)
    adapters.CALLS.clear()
    change(dispatch_permit, "enter_recovery")
    change(dispatch_permit, "exit_recovery")
    # Same authority and allowed class again, but a retired generation: no write, no call.
    from cloudops.features.investigations.collector import Interrupted

    with pytest.raises(Interrupted):
        collect_checkpointed(claim, context, bound, GATE)
    assert adapters.CALLS == {}
    late = adapters.state(context, 5)

    def write(db, carry):
        from cloudops.features.investigations.storage import store_source

        return store_source(db, claim.subject_id, context.workload_uid, late, carry)

    assert not checkpoint(claim, "collect_source:kubernetes.state", GATE, write)
    assert not finish(claim, HANDLERS[claim.kind], error="late", gate=GATE)
    view = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert view["evidence"] == [] and view["state"] == "collecting"


def test_database_values_alone_never_grant_dispatch(client, dispatch_permit):
    create(client, sign_in(client), incident_body(client))
    claim = claim_next(HANDLERS, GATE)
    change(dispatch_permit, "enter_recovery")
    current = GATE.current()[0]
    with sessions()() as db, db.begin():
        db.execute(
            update(JobAttempt)
            .where(JobAttempt.lease_token == claim.token)
            .values(permit_authority=current.authority_id, permit_generation=current.generation)
        )
    forged = Claim(**{**claim.__dict__, "permit_generation": current.generation})
    assert not checkpoint(forged, "collect_source:kubernetes.state", GATE, lambda db, carry: carry)
    assert not finish(forged, HANDLERS[forged.kind], error="forged", gate=GATE)


def test_renewal_keeps_in_flight_work_publishable(client, dispatch_permit):
    ids = create(client, sign_in(client), incident_body(client)).json()
    claim = claim_next(HANDLERS, GATE)
    change(dispatch_permit, "renew", days=90)
    result = collect_checkpointed(claim, context_for(claim), {}, GATE)
    assert finish(claim, HANDLERS[claim.kind], result=result, gate=GATE)
    view = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}").json()
    assert view["state"] == "insufficient_evidence" and view["dispatch"] is None


def test_restored_approval_stays_denied_and_cannot_regain_write_authority(
    permission_context, dispatch_permit, monkeypatch
):
    c = permission_context
    execution_id = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id)
        row.state, row.holds_lock, row.maintenance_released = "queued", True, False
        # The restored approval is still within its plan window; only authority can stop it.
        db.get(ActionPlan, row.plan_id).expires_at = database_now(db) + timedelta(minutes=5)
        db.add(Job(kind="recovery_execution_v1", subject_id=execution_id))
    change(dispatch_permit, "enter_recovery")
    calls = []

    def quarantined(path, body=None):
        calls.append((path, body))
        # The executor retired every target epoch on its own enter_recovery/startup.
        raise DomainError(409, "target_quarantined", "Target admission is closed.")

    monkeypatch.setattr(recovery.client, "gateway", quarantined)
    assert claim_next(HANDLERS, GATE) is None
    change(dispatch_permit, "resume", resume="collection")
    while run_once():
        pass  # Only the separately deferred collection fixture may run.
    assert calls == []
    with sessions()() as db:
        assert db.get(Execution, execution_id).state == "queued"
    change(dispatch_permit, "resume", resume="recovery_execution")
    assert run_once()
    assert [path for path, _ in calls] == ["/dispatch"]
    with sessions()() as db:
        row = db.get(Execution, execution_id)
        assert row.result["outcome"] == "outcome_unknown" and row.holds_lock
