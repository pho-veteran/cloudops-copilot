"""DispatchPermit/v1 file authority: atomic monotonic writes, fail-closed reads, CLI actor."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from cloudops.entrypoints import dispatch_admin
from cloudops.entrypoints.dispatch import CLASSES, PERMIT_CLASSES
from cloudops.shared import dispatch_permit as permits
from cloudops.shared.file_lock import file_lock

ACTOR = {"uid": None, "channel": "unit_test"}


def issue(root, **kwargs):
    return permits.bootstrap(
        root, CLASSES, reason="Fresh install", evidence=["unit"], actor=ACTOR, **kwargs
    )


def change(root, operation, **kwargs):
    return permits.transition(
        root, operation, CLASSES, reason="Recovery drill", evidence=["unit"], actor=ACTOR, **kwargs
    )


def test_bootstrap_issues_every_class_once_and_refuses_a_second_authority(tmp_path):
    permit = issue(tmp_path)
    assert (permit.generation, permit.mode, permit.operation) == (1, "normal", "bootstrap")
    assert permit.allowed_job_kinds == tuple(CLASSES)
    document = json.loads((tmp_path / "permit.json").read_text())
    assert document["schema"] == "DispatchPermit/v1"
    assert set(document) == {
        "schema",
        "authority_id",
        "generation",
        "mode",
        "allowed_job_kinds",
        "valid_until",
        "issued_at",
        "operation",
    }
    with pytest.raises(permits.PermitError):
        issue(tmp_path)
    # Deleting the permit does not make the host look freshly installed again.
    (tmp_path / "permit.json").unlink()
    with pytest.raises(permits.PermitError):
        issue(tmp_path)


def test_mode_changes_increment_generation_and_renewal_keeps_it(tmp_path):
    first = issue(tmp_path)
    recovery = change(tmp_path, "enter_recovery")
    assert (recovery.generation, recovery.mode, recovery.allowed_job_kinds) == (2, "recovery", ())
    assert recovery.authority_id == first.authority_id
    resumed = change(tmp_path, "resume", resume="collection")
    assert (resumed.generation, resumed.allowed_job_kinds) == (3, ("collection",))
    with pytest.raises(permits.PermitError):
        change(tmp_path, "resume", resume="collection")
    with pytest.raises(permits.PermitError):
        change(tmp_path, "resume", resume="unknown")
    renewed = change(tmp_path, "renew", days=60)
    assert renewed.generation == 3 and renewed.valid_until > resumed.valid_until
    normal = change(tmp_path, "exit_recovery")
    assert (normal.generation, normal.mode, normal.allowed_job_kinds) == (
        4,
        "normal",
        tuple(CLASSES),
    )
    with pytest.raises(permits.PermitError):
        change(tmp_path, "exit_recovery")
    audit = permits.audit_tail(tmp_path)
    assert [entry["operation"] for entry in audit] == [
        "bootstrap",
        "enter_recovery",
        "resume",
        "renew",
        "exit_recovery",
    ]
    assert not (tmp_path / "permit.json.tmp").exists()


def test_justification_and_validity_are_bounded(tmp_path):
    with pytest.raises(permits.PermitError):
        permits.bootstrap(tmp_path, CLASSES, reason=" ", evidence=["x"], actor=ACTOR)
    with pytest.raises(permits.PermitError):
        permits.bootstrap(tmp_path, CLASSES, reason="ok", evidence=[], actor=ACTOR)
    with pytest.raises(permits.PermitError):
        issue(tmp_path, days=366)
    assert not (tmp_path / "permit.json").exists()


def kinds(gate):
    return gate.current()[1], gate.current()[2]


def test_gate_fails_closed_for_missing_invalid_stale_and_denied_permits(tmp_path):
    gate = permits.DispatchGate(PERMIT_CLASSES, tmp_path)
    assert kinds(gate) == (frozenset(), "permit_missing")
    with gate.admitted() as admission:
        assert admission.kinds == frozenset() and admission.reason == "permit_missing"
    permit = issue(tmp_path)
    assert kinds(gate) == (frozenset(PERMIT_CLASSES), None)
    assert gate.allows("development_collection_v1", permit.authority_id, permit.generation)
    assert not gate.allows("development_collection_v1", permit.authority_id, 2)
    assert not gate.allows("unregistered_v1", permit.authority_id, permit.generation)

    path = tmp_path / "permit.json"
    original = path.read_bytes()
    path.write_text('{"schema": "DispatchPermit/v1"}')
    assert kinds(gate) == (frozenset(), "permit_invalid")
    path.write_bytes(b" " * (permits.MAX_PERMIT_BYTES + 1))
    assert kinds(gate) == (frozenset(), "permit_invalid")
    document = json.loads(original)
    document["valid_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(document))
    assert kinds(gate) == (frozenset(), "permit_stale")
    document["valid_until"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    document["extra"] = "field"
    path.write_text(json.dumps(document))
    assert kinds(gate) == (frozenset(), "permit_invalid")
    path.write_bytes(original)

    change(tmp_path, "enter_recovery")
    assert kinds(gate) == (frozenset(), "recovery_mode")
    assert gate.status("development_collection_v1") == (False, "recovery_mode")
    change(tmp_path, "resume", resume="recommendation")
    assert kinds(gate) == (frozenset({"recommendation_report_v1"}), None)
    assert gate.status("development_collection_v1") == (False, "recovery_mode")
    assert gate.status("recommendation_report_v1") == (True, None)


def test_restored_older_permit_file_is_stale_for_a_reader_that_saw_newer(tmp_path):
    issue(tmp_path)
    old = (tmp_path / "permit.json").read_bytes()
    change(tmp_path, "enter_recovery")
    change(tmp_path, "exit_recovery")
    gate = permits.DispatchGate(PERMIT_CLASSES, tmp_path)
    assert kinds(gate)[1] is None
    (tmp_path / "permit.json").write_bytes(old)
    assert kinds(gate) == (frozenset(), "permit_stale")


def test_writer_waits_for_a_reader_holding_the_shared_boundary(tmp_path):
    issue(tmp_path)
    gate = permits.DispatchGate(PERMIT_CLASSES, tmp_path)
    entered, release = threading.Event(), threading.Event()
    applied = []

    def reader():
        with gate.admitted() as admission:
            assert admission.permit.generation == 1
            entered.set()
            release.wait(5)

    def writer():
        applied.append(change(tmp_path, "enter_recovery").generation)

    holder = threading.Thread(target=reader)
    holder.start()
    entered.wait(5)
    other = threading.Thread(target=writer)
    other.start()
    time.sleep(0.3)
    assert applied == []
    release.set()
    holder.join(5)
    other.join(5)
    assert applied == [2]
    with gate.admitted() as admission:
        assert admission.kinds == frozenset() and admission.reason == "recovery_mode"


def test_lock_timeout_denies_instead_of_waiting_forever(tmp_path):
    issue(tmp_path)
    gate = permits.DispatchGate(PERMIT_CLASSES, tmp_path, timeout=0.2)
    with file_lock(tmp_path / "permit.lock", exclusive=True):
        result = []

        def read():
            with gate.admitted() as admission:
                result.append(admission.reason)

        thread = threading.Thread(target=read)
        thread.start()
        thread.join(5)
    assert result == ["permit_invalid"]


def test_cli_requires_the_trusted_host_and_a_configured_root(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JOB_DISPATCH_ROOT", raising=False)
    with pytest.raises(SystemExit):
        dispatch_admin.main(["status"])
    assert "JOB_DISPATCH_ROOT" in capsys.readouterr().err
    monkeypatch.setenv("JOB_DISPATCH_ROOT", str(tmp_path))
    dispatch_admin.main(["status"])
    status = json.loads(capsys.readouterr().out)
    assert status["permit"] is None and status["denial"] == "permit_missing"
    if not hasattr(__import__("os"), "geteuid"):
        with pytest.raises(SystemExit):
            dispatch_admin.main(
                ["bootstrap", "--fresh-install", "--reason", "x", "--evidence", "y"]
            )
        assert "Linux deployment host" in capsys.readouterr().err
        assert not (tmp_path / "permit.json").exists()


def test_cli_rejects_a_non_allowlisted_or_wrong_owner_actor(tmp_path, monkeypatch):
    import os

    if not hasattr(os, "geteuid"):
        pytest.skip("OS actor ownership checks run on the Linux deployment host.")
    monkeypatch.setenv("JOB_DISPATCH_ADMIN_UIDS", str(os.geteuid() + 1))
    with pytest.raises(permits.PermitError):
        dispatch_admin.trusted_actor(tmp_path, create=False)
    monkeypatch.setenv("JOB_DISPATCH_ADMIN_UIDS", str(os.geteuid()))
    tmp_path.chmod(0o777)
    with pytest.raises(permits.PermitError):
        dispatch_admin.trusted_actor(tmp_path, create=False)
    tmp_path.chmod(0o755)
    assert dispatch_admin.trusted_actor(tmp_path, create=False)["uid"] == os.geteuid()
