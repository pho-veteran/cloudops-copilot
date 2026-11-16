"""Restore cleanup SQL and preservation with explicit controlled external authority evidence."""

import copy
import json
import os
import socket
import threading
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, func, text
from test_dispatch_gate import change
from test_permissions import permission_context as _permission_context
from test_recovery_lifecycle import approve

from cloudops.features.access.models import LoginSession
from cloudops.features.recovery.models import ActionPlan, Execution, PlanReview
from cloudops.features.recovery.restore import closed_gateway_targets, retire
from cloudops.entrypoints import recovery_restore
from cloudops.shared.database import BACKEND_ROOT, engine, sessions
from cloudops.shared.dispatch_permit import PermitError
from cloudops.shared.jobs import Job

permission_context = _permission_context


def test_restored_unmarked_snapshot_cannot_establish_not_sent(permission_context):
    c = permission_context
    identifier = approve(c)
    with sessions()() as db:
        plan = db.get(ActionPlan, UUID(c.consumer.plan_id))
        original = (copy.deepcopy(plan.content), plan.digest, plan.id)
        request = copy.deepcopy(db.get(Execution, identifier).dispatch_request)
    with sessions()() as db, db.begin():
        result = retire(db)
    assert result["sessions_revoked"] > 0 and result["reviews_retired"] > 0
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.outcome.attribution == "outcome_unknown" and row.holds_lock
        assert row.dispatch_started_at is not None and row.dispatch_request == request
        assert row.state == "outcome_unknown" and row.finished_at is not None
        assert not row.maintenance_released
        assert db.scalar(select(Job).where(Job.subject_id == identifier)).state == "failed"
        plan = db.get(ActionPlan, UUID(c.consumer.plan_id))
        assert (plan.content, plan.digest, plan.id) == original and plan.state == "invalidated"
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 0
        assert db.scalar(select(func.count()).select_from(PlanReview)) == 0
    assert c.actors["operator"].client.get("/api/session").status_code == 401


@pytest.mark.parametrize("boundary", ["host_open", "gateway_unavailable", "closed"])
def test_restore_entrypoint_requires_both_closed_authorities(
    permission_context, dispatch_permit, monkeypatch, capsys, boundary
):
    c = permission_context
    identifier = approve(c)
    monkeypatch.setenv("JOB_DISPATCH_ADMIN_UIDS", str(os.geteuid()))
    if boundary != "host_open":
        change(dispatch_permit, "enter_recovery")

    def targets():
        if boundary == "gateway_unavailable":
            raise RuntimeError("Controlled gateway unavailable; no cleanup.")
        return [{"uid": c.consumer.uid}, {"uid": "configured-target-absent-from-restored-db"}]

    monkeypatch.setattr(recovery_restore, "closed_gateway_targets", targets)
    if boundary == "closed":
        recovery_restore.main()
        result = json.loads(capsys.readouterr().out)
        assert result["configured_gateway_targets_closed"] == 2
        assert result["executions_retired"] == 1
        assert c.actors["operator"].client.get("/api/session").status_code == 401
    else:
        with pytest.raises(PermitError if boundary == "host_open" else RuntimeError):
            recovery_restore.main()
        with sessions()() as db:
            row = db.get(Execution, identifier)
            assert row.state == "queued" and row.dispatch_started_at is None
            assert db.get(ActionPlan, row.plan_id).state == "approved"
            assert db.scalar(select(func.count()).select_from(LoginSession)) > 0


def test_lifecycle_migration_preserves_legacy_bytes_and_adopts_calls_conservatively(
    permission_context,
):
    identifier = UUID(permission_context.consumer.execution_id)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    try:
        command.downgrade(config, "0013_recovery_provenance")
        with engine().begin() as connection:
            connection.execute(
                text(
                    "UPDATE recovery_executions SET state='executing', holds_lock=true WHERE id=:id"
                ),
                {"id": identifier},
            )
            before = connection.execute(
                text(
                    "SELECT id,plan_id,result::text,state,holds_lock FROM recovery_executions WHERE id=:id"
                ),
                {"id": identifier},
            ).one()
            plans = connection.execute(
                text("SELECT id,content::text,digest FROM action_plans ORDER BY id")
            ).all()
        command.upgrade(config, "head")
        with engine().connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT id,plan_id,result::text,state,holds_lock FROM recovery_executions WHERE id=:id"
                    ),
                    {"id": identifier},
                ).one()
                == before
            )
            assert (
                connection.execute(
                    text("SELECT id,content::text,digest FROM action_plans ORDER BY id")
                ).all()
                == plans
            )
            captured = connection.execute(
                text(
                    "SELECT dispatch_started_at,reconcile_deadline,reconcile_attempts FROM recovery_executions WHERE id=:id"
                ),
                {"id": identifier},
            ).one()
            assert captured[0] is not None and captured[1] > captured[0] and captured[2] == 0
        command.upgrade(config, "head")
        with engine().connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT dispatch_started_at,reconcile_deadline,reconcile_attempts FROM recovery_executions WHERE id=:id"
                    ),
                    {"id": identifier},
                ).one()
                == captured
            )
        with pytest.raises(RuntimeError, match="Retained call admission"):
            command.downgrade(config, "0013_recovery_provenance")
    finally:
        command.upgrade(config, "head")


@pytest.mark.parametrize(
    "response",
    [
        {"targets": []},
        {"status": "denied", "code": "maintenance_actor_denied"},
        {"targets": [{"uid": "fixture", "enabled": True, "recovery_mode": True}]},
    ],
)
def test_restore_rejects_unproven_gateway_summary(tmp_path, monkeypatch, response):
    monkeypatch.setenv("EXECUTOR_ROOT", str(tmp_path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(tmp_path / "maintenance.sock"))
    server.listen(1)
    server.settimeout(3)

    def reply():
        with server:
            connection, _ = server.accept()
            with connection:
                connection.makefile("rb").readline(4096)
                connection.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="Keep every configured gateway target closed"):
            closed_gateway_targets()
    finally:
        thread.join(timeout=4)
