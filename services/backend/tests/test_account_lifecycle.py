import io
import re
import sys

import pytest
from sqlalchemy import func, select

from cloudops.entrypoints.cli import main
from cloudops.features.access.models import LoginSession, User
from cloudops.features.access.service import (
    authenticate,
    login,
    provision,
    revoke_sessions,
    set_role,
)
from cloudops.shared.errors import DomainError

PASSWORD = "local-test-password"


def account_state(db, username):
    """Read-only probe: ends any transaction so the next read sees other sessions' commits."""
    db.rollback()
    user = db.scalar(select(User).where(User.username == username))
    state = None if user is None else (user.password_hash, user.id, user.role)
    db.rollback()
    return state


def sessions_for(db, username):
    db.rollback()
    count = db.scalar(
        select(func.count())
        .select_from(LoginSession)
        .join(User, User.id == LoginSession.user_id)
        .where(User.username == username)
    )
    db.rollback()
    return count


def assert_expired(db, token):
    db.rollback()
    with pytest.raises(DomainError) as err:
        authenticate(db, token)
    assert err.value.status == 401
    assert err.value.code == "session_expired"
    db.rollback()


def test_set_role_preserves_hash_id_and_revokes_sessions(db):
    provision(db, "operator", PASSWORD, "operator")
    token, _ = login(db, "operator", PASSWORD, None)
    before_hash, before_id, _ = account_state(db, "operator")

    set_role(db, "operator", "viewer")

    after_hash, after_id, after_role = account_state(db, "operator")
    assert (after_hash, after_id) == (before_hash, before_id)
    assert after_role == "viewer"
    assert_expired(db, token)
    _, session_view = login(db, "operator", PASSWORD, None)
    assert session_view.principal.role == "viewer"
    assert session_view.principal.id == before_id


def test_set_role_invalid_role_raises_and_changes_nothing(db):
    provision(db, "operator", PASSWORD, "operator")
    login(db, "operator", PASSWORD, None)
    login(db, "operator", PASSWORD, None)
    before = account_state(db, "operator")

    with pytest.raises(DomainError) as err:
        set_role(db, "operator", "admin")

    assert err.value.status == 422
    assert err.value.code == "invalid_role"
    assert account_state(db, "operator") == before
    assert sessions_for(db, "operator") == 2


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda db: set_role(db, "ghost", "viewer"), id="set_role"),
        pytest.param(lambda db: revoke_sessions(db, "ghost"), id="revoke_sessions"),
    ],
)
def test_set_role_unknown_account_raises_and_changes_nothing(db, operation):
    with pytest.raises(DomainError) as err:
        operation(db)

    assert err.value.status == 404
    assert err.value.code == "account_not_found"
    assert db.scalar(select(func.count()).select_from(User)) == 0
    assert db.scalar(select(func.count()).select_from(LoginSession)) == 0


def test_revoke_sessions_preserves_password_role_id(db):
    provision(db, "operator", PASSWORD, "operator")
    first, _ = login(db, "operator", PASSWORD, None)
    second, _ = login(db, "operator", PASSWORD, None)
    before = account_state(db, "operator")

    revoke_sessions(db, "operator")

    assert_expired(db, first)
    assert_expired(db, second)
    assert sessions_for(db, "operator") == 0
    assert account_state(db, "operator") == before
    _, session_view = login(db, "operator", PASSWORD, None)
    assert session_view.principal.role == "operator"
    assert session_view.principal.id == before[1]


def test_revoke_sessions_spares_other_users(db):
    provision(db, "operator", PASSWORD, "operator")
    provision(db, "viewer", PASSWORD, "viewer")
    operator_token, _ = login(db, "operator", PASSWORD, None)
    viewer_token, _ = login(db, "viewer", PASSWORD, None)

    revoke_sessions(db, "operator")

    assert_expired(db, operator_token)
    viewer_session = authenticate(db, viewer_token)
    assert viewer_session.principal.username == "viewer"
    db.rollback()
    assert sessions_for(db, "viewer") == 1
    assert sessions_for(db, "operator") == 0


def test_set_role_same_role_still_revokes(db):
    provision(db, "operator", PASSWORD, "operator")
    token, _ = login(db, "operator", PASSWORD, None)
    before = account_state(db, "operator")

    set_role(db, "operator", "operator")

    assert_expired(db, token)
    assert account_state(db, "operator") == before
    assert sessions_for(db, "operator") == 0


def run_cli(monkeypatch, argv, stdin=""):
    monkeypatch.setattr(sys, "argv", ["cli", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    main()


def test_cli_account_role_updates_role_and_output(db, monkeypatch, capsys):
    provision(db, "operator", PASSWORD, "operator")
    first, _ = login(db, "operator", PASSWORD, None)
    second, _ = login(db, "operator", PASSWORD, None)
    before = account_state(db, "operator")

    run_cli(monkeypatch, ["account-role", "operator", "viewer"])

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines() == ["Account role updated; previous sessions revoked."]
    assert captured.err == ""
    after = account_state(db, "operator")
    assert (after[0], after[1], after[2]) == (before[0], before[1], "viewer")
    assert_expired(db, first)
    assert_expired(db, second)


def test_cli_revoke_sessions_kicks_only_target(db, monkeypatch, capsys):
    provision(db, "operator", PASSWORD, "operator")
    provision(db, "viewer", PASSWORD, "viewer")
    operator_token, _ = login(db, "operator", PASSWORD, None)
    viewer_token, _ = login(db, "viewer", PASSWORD, None)
    before = account_state(db, "operator")

    run_cli(monkeypatch, ["revoke-sessions", "operator"])

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines() == ["Sessions revoked."]
    assert captured.err == ""
    assert_expired(db, operator_token)
    assert authenticate(db, viewer_token).principal.username == "viewer"
    assert account_state(db, "operator") == before


@pytest.mark.parametrize(
    "argv",
    [["account-role", "ghost", "viewer"], ["revoke-sessions", "ghost"]],
    ids=["account-role", "revoke-sessions"],
)
def test_cli_new_commands_unknown_account_fail_cleanly(db, monkeypatch, capsys, argv):
    with pytest.raises(SystemExit) as err:
        run_cli(monkeypatch, argv)

    assert err.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert re.match(r"^error: ", lines[0])
    assert "Traceback" not in captured.err
    assert db.scalar(select(func.count()).select_from(User)) == 0
    assert db.scalar(select(func.count()).select_from(LoginSession)) == 0


def test_cli_legacy_reset_rejects_short_password_cleanly(db, monkeypatch, capsys):
    provision(db, "operator", PASSWORD, "operator")
    before = account_state(db, "operator")

    with pytest.raises(SystemExit) as err:
        run_cli(
            monkeypatch,
            ["account", "operator", "--role", "viewer", "--password-stdin"],
            "short\n",
        )

    assert err.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error: ")
    assert "12..256" in lines[0]
    assert "Traceback" not in captured.err
    assert account_state(db, "operator") == before


def test_cli_legacy_account_reset_still_works(db, monkeypatch, capsys):
    provision(db, "viewer", PASSWORD, "viewer")
    old_token, _ = login(db, "viewer", PASSWORD, None)
    before_id = account_state(db, "viewer")[1]

    run_cli(
        monkeypatch,
        ["account", "viewer", "--role", "operator", "--password-stdin"],
        "new-password-123456\n",
    )

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines() == ["Account provisioned; previous sessions revoked."]
    assert captured.err == ""
    assert_expired(db, old_token)
    _, session_view = login(db, "viewer", "new-password-123456", None)
    assert session_view.principal.role == "operator"
    assert session_view.principal.id == before_id
    with pytest.raises(DomainError) as err:
        login(db, "viewer", PASSWORD, None)
    assert err.value.status == 401
