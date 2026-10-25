# Account Lifecycle CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the maintenance CLI an explicit role-only change command and an explicit all-sessions revocation command — passwords and UUIDs preserved, role change + session revocation committed atomically, unknown accounts/invalid roles failing cleanly — per issue #17.

**Architecture:** Extend `features/access/service.py` with `set_role` / `revoke_sessions` using the same row-lock + single-transaction pattern as `login()`/`provision()`, wire two flat argparse subcommands in `entrypoints/cli.py` behind a one-line error surface, and cover behavior with one new PostgreSQL+CLI test file. No migration, no transport contract change.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 ORM, argparse, pytest against a session-scoped disposable `_test` PostgreSQL (Compose), Ruff.

**Spec:** `docs/superpowers/specs/2026-10-25-account-lifecycle-design.md` (implements GitHub issue #17; the spec travels with this plan).

## Global Constraints

- Tests run only against a database whose URL ends in `_test`; never application data (README/CONTRIBUTING). Target test file: `services/backend/tests/test_account_lifecycle.py`.
- Exactly two roles (`viewer`, `operator`); no third role, signup, admin UI, IDP, migration or account status (issue #17 non-goals).
- The new service functions take **no password parameter**; password policy/`provision()` path stays untouched.
- Revocation wording: post-commit authenticating requests get `401 session_expired`; never claim or test retroactive cancellation of pre-commit authenticated requests or already admitted jobs.
- No HTTP route/Pydantic schema change → no OpenAPI/TS regeneration. Exception: if shared schemas end up touched, run `uv run --project services/backend python scripts/contracts.py --write`.
- English for all CLI copy and docs; passwords/tokens are never printed or logged.
- Conventional Commits with lowercase scope, branch from current `main`, PR links issue with `Related to #17` (CONTRIBUTING).
- Legacy command compatibility: `account <username> --role ... [--password-stdin]` behavior and its exact success line `Account provisioned; previous sessions revoked.` are pinned.

## Review Focus

- Unknown username on a new command → clean one-line error (exit nonzero), zero rows created or changed — pinned by Task 2 CLI test.
- Another user's active sessions must survive a role change/revocation of a different account — pinned by Task 1 test.
- Same-role reissue still commits and revokes (deliberate no-special-casing; nobody "fixes" it into a no-op) — pinned by Task 1 test.
- The revocation boundary: only post-commit authentications die; tests state the transaction boundary — pinned by Task 3.
- Bad input on the *legacy* `account` command (e.g. short password) fails as a clean one-liner after D3 broadens the catch — pinned by Task 2 test so nobody reintroduces tracebacks.

---

### Task 1: Service layer — `set_role` and `revoke_sessions`

**Files:**
- Modify: `services/backend/src/cloudops/features/access/service.py` (after `provision`)
- Test: `services/backend/tests/test_account_lifecycle.py` (create)

**Interfaces:**
- Consumes: existing `provision(db, username, password, role)`, `login(db, username, password, old_token) -> (token, SessionView)`, `authenticate(db, token) -> SessionView`, `DomainError(status, code, message)` from shared/errors.
- Produces (all later tasks depend on these exact names):
  - `set_role(db: Session, username: str, role: str) -> None` — raises `DomainError(404, "account_not_found", …)` for unknown username; `DomainError(422, "invalid_role", …)` for any role outside `{viewer, operator}`. Inside one `db.begin()` transaction: locks the `User` row `with_for_update()`, updates `user.role` only, deletes all `LoginSession` rows of that user. Message wording is the builder's choice; the code strings `"account_not_found"` / `"invalid_role"` are pinned.
  - `revoke_sessions(db: Session, username: str) -> None` — same unknown-account error; deletes only that user's `LoginSession` rows inside one transaction; touches no user field.

- [ ] **Step 0 (setup): verify the isolated workspace** — the worktree `C:\Users\Lenovo LOQ\.local\share\opencode\worktree\778f3d\e01-account-lifecycle` already carries branch `feat/e01-account-lifecycle` (base commit `0e45b4d`, current `main` tip). Confirm with `git branch --show-current`; no branch creation needed. The design/plan docs at `docs/superpowers/` are committed on this branch before Task 1's test commit.

- [ ] **Step 1: Write the failing tests** in `services/backend/tests/test_account_lifecycle.py`

Use the existing fixtures: `db`, plus `provision(...)` directly (password `"local-test-password"`, usernames `operator`/`viewer`). Import style follows `tests/test_access_admission.py`.

```python
# canonical shape — every test states behavior, in this style
def test_set_role_preserves_hash_id_and_revokes_sessions(db):
    provision(db, "operator", "local-test-password", "operator")  # 12..256 chars
    token, _ = login(db, "operator", "local-test-password", None)
    before_hash, before_id = user_snapshot(db, "operator")        # small local helper
    set_role(db, "operator", "viewer")
    assert (before_hash, before_id) == user_snapshot(db, "operator")
    with pytest.raises(DomainError) as err:
        authenticate(db, token)                                   # post-commit 401
    assert err.value.status == 401 and err.value.code == "session_expired"
    # unchanged password now authenticates as the new role
    _, session_view = login(db, "operator", "local-test-password", None)
    assert session_view.principal.role == "viewer"
```

Required tests (names + the assertion each one makes):

1. `test_set_role_preserves_hash_id_and_revokes_sessions` — SHA-based hash bytes and UUID unchanged; pre-change token authenticating after commit raises `DomainError(401, "session_expired")`; password re-login succeeds with the new role.
2. `test_set_role_invalid_role_raises_and_changes_nothing` — `set_role(db, "operator", "admin")` raises `DomainError` with code `"invalid_role"`; username row untouched (role still `operator`, same hash) and zero session rows deleted.
3. `test_set_role_unknown_account_raises_and_changes_nothing` — parametrize over `set_role` and `revoke_sessions`: unknown username raises `DomainError` code `"account_not_found"`; `users` count stays 0; no new rows anywhere.
4. `test_revoke_sessions_preserves_password_role_id` — two tokens revoked (`authenticate` → 401 both); re-login with the *same* password returns role `operator`; UUID unchanged.
5. `test_revoke_sessions_spares_other_users` — operator and viewer both logged in; `revoke_sessions(db, "operator")`; the viewer's token still authenticates (`SessionView` returned untouched), operator's token 401s.
6. `test_set_role_same_role_still_revokes` — `set_role(db, "operator", "operator")` still deletes that account's tokens and leaves password/role/UUID identical otherwise.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests/test_account_lifecycle.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'set_role'`.

- [ ] **Step 3: Implement** `set_role` / `revoke_sessions` in `service.py`

Same transaction/locking shape as `provision()` (`select(User).where(...).with_for_update()` inside `with db.begin():`). `set_role` validates the role first (before any mutation), raising the pinned error codes. `revoke_sessions` is a `delete(LoginSession).where(user_id == …)`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --project services/backend pytest services/backend/tests/test_account_lifecycle.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add services/backend/src/cloudops/features/access/service.py services/backend/tests/test_account_lifecycle.py
git commit -m "feat(auth): add role-only and session-revocation service operations"
```

### Task 2: CLI wiring and one-line error surface

**Files:**
- Modify: `services/backend/src/cloudops/entrypoints/cli.py`
- Test: `services/backend/tests/test_account_lifecycle.py` (append; reuse Task 1 fixtures)

**Interfaces:**
- Consumes: `set_role` / `revoke_sessions` (Task 1), `DomainError`/`ValueError`.
- Produces: two argparse subcommands —
  - `account-role <username> <role>` with role `choices=["viewer", "operator"]` (argparse rejects bad roles with its own exit 2; the service-level check is the library-facing guard).
  - `revoke-sessions <username>`.
  - Both dispatch as `main()` branches, print exactly one short success line to stdout, and exit 0. On `DomainError` or `ValueError` anywhere in the dispatch, `main()` prints `error: <message>` (one line; `DomainError.message` or `str(exc)`) to stderr and exits 1 — no traceback. Argparse `SystemExit(2)` behavior is untouched.
  - `account` subcommand behavior/strings unchanged.

- [ ] **Step 1: Write the failing tests** (append to `test_account_lifecycle.py`) — invoke `cloudops.entrypoints.cli.main()` with `sys.argv` monkeypatched in-process; capture with `capsys`; expect `SystemExit` on errors.

Required tests:

1. `test_cli_account_role_updates_role_and_output` — argv `["cli", "account-role", "operator", "viewer"]` after provisioning an operator with two live sessions; exit 0; one stdout line; role now `viewer`; old tokens 401; hash unchanged.
2. `test_cli_revoke_sessions_kicks_only_target` — argv `["cli", "revoke-sessions", "operator"]` with a second viewer session alive; operator tokens 401, viewer token untouched; role and hash unchanged.
3. `test_cli_new_commands_unknown_account_fail_cleanly` — parametrize both commands with a username that does not exist: `SystemExit(1)`, stderr matches `^error:` (single line, no traceback), zero `users` rows before and after.
4. `test_cli_legacy_reset_rejects_short_password_cleanly` — legacy `account operator --role viewer` with stdin `short` → `SystemExit(1)` + one stderr line naming the 12..256 policy; account unchanged (pins Review Focus 5).
5. `test_cli_legacy_account_reset_still_works` — legacy `account viewer --role operator` with stdin `new-password-123456` → exit 0; exact stdout `Account provisioned; previous sessions revoked.`; new password accepted, old revoked, role changed (pins compatibility).

- [ ] **Step 2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests/test_account_lifecycle.py -k cli -q`
Expected: FAIL (`SystemExit(2)` from argparse "unrecognized arguments" — the commands do not exist yet).

- [ ] **Step 3: Implement** in `cli.py`

Add the two `sub.add_parser(...)` definitions; branch dispatch after the existing pattern; wrap the whole dispatch in `try / except (DomainError, ValueError)` → stderr one-liner + `sys.exit(1)`. Keep `getpass`/`--password-stdin` handling exclusive to `account`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --project services/backend pytest services/backend/tests/test_account_lifecycle.py -q`
Expected: PASS (11 tests total).

- [ ] **Step 5: Commit**

```bash
git add services/backend/src/cloudops/entrypoints/cli.py services/backend/tests/test_account_lifecycle.py
git commit -m "feat(auth): expose account-role and revoke-sessions in maintenance CLI"
```

### Task 3: Revocation boundary under a concurrent login

**Files:**
- Test: `services/backend/tests/test_account_lifecycle.py` (append)
- Modify: `services/backend/src/cloudops/features/access/service.py` — **only if** the interleaving reveals a needed adjustment (the `with_for_update()` pattern in D2 should make it pass as-is).

**Interfaces:**
- Consumes: `set_role`, `login`, `authenticate`, an independent second connection via `cloudops.shared.database.sessions` (as `conftest.py` does).

- [ ] **Step 1: Write the failing-complete test**

`test_role_change_serializes_with_concurrent_login` — the test's comments must state the boundary verbatim: *"post-commit authentication only; no retroactive cancellation is claimed"*.

Concrete interleave (mechanism may differ, but all these assertions stay):

1. Connection 1: provision normally via the `db` fixture first; then connection 1 opens its own transaction and performs a `login(...)`-shaped sequence: `SELECT … FOR UPDATE` on the `operator` user row, insert one `LoginSession` (token A), **keeps the transaction open**.
2. Submit `set_role` for that same account on connection 2 (a plain thread/executor submission with a bounded wait is acceptable; release connection 1's lock promptly by committing it).
3. Connection 1 commits first → token A's row is durable for the *old* state; then connection 2's `set_role` commits.
4. Assert the final consistent state: `authenticate(token A)` → 401 (A's row was destroyed by the committed revocation), role is `viewer`, zero sessions left for the account, hash/UUID untouched.

If PostgreSQL raises a lock timeout instead of cleanly waiting, prefer just committing connection 1 promptly (the ORM's `db.begin()` already gives clean ordering); the property under test is serial commit order + atomic revocation, not deadlock behavior.

- [ ] **Step 2: Run to verify pass/failure diagnosis**

Run: `uv run --project services/backend pytest services/backend/tests/test_account_lifecycle.py::test_role_change_serializes_with_concurrent_login -q`
Expected: PASS with the D2 pattern; a FAIL here means the implementation re-checks, never that the boundary claim is loosened.

- [ ] **Step 3: Commit**

```bash
git add services/backend/tests/test_account_lifecycle.py
git commit -m "test(auth): pin revocation boundary with concurrent login"
```

### Task 4: Operator documentation and full verification

**Files:**
- Modify: `docs/foundation.md` — § "Configuration and account operations" (shell block near line 40)
- Modify: `README.md` — the quick-start paragraph that currently ends "Provisioning an existing username resets its password/role and revokes its sessions." (near line 26)

**Interfaces:**
- Consumes: the final CLI surface from Task 2 (`account`, `account-role`, `revoke-sessions`).
- Produces: the documented operator syntax referenced by issue #17's acceptance item 5 (the exercise may use *only* these documented commands).

- [ ] **Step 1: Update the `docs/foundation.md` command block**

Keep the existing two `account` lines and add, in the same block, the two new commands with one-line comments stating what each preserves:

```sh
# Change only the role: keeps the stored password and account ID; previous sessions of that account are revoked.
docker compose exec api python -m cloudops.entrypoints.cli account-role operator operator
# Revoke all active sessions of one account: keeps its password and role.
docker compose exec api python -m cloudops.entrypoints.cli revoke-sessions operator
```

Add one prose sentence: failures for unknown accounts or invalid roles are clean errors and change no data; sessions authenticated before a revocation committed are not claimed to be cancelled.

- [ ] **Step 2: Add one README sentence** in the quick-start account paragraph pointing at the new commands (role-only change and full-session revocation never touch the stored password).

- [ ] **Step 3: Run the full backend verification**

Run: `./scripts/check-backend.sh` (Bash-capable shell; creates/reuses `cloudops_test` only; suite may take ~1 minute because of the interruption-lease test)
Expected: green (lint/format + PostgreSQL suite including the new file).
Also run: `uv run --project services/backend ruff check --config services/backend/pyproject.toml services/backend scripts` and the matching `ruff format --check`.
Expected: clean.
Contracts: none regenerated (no Pydantic change); CI's drift check must stay green.

- [ ] **Step 4: Commit**

```bash
git add docs/foundation.md README.md
git commit -m "docs(auth): publish role-only change and session revocation commands"
```

### Task 5: Isolated operator exercise and PR evidence

**Files:**
- Create: no repo files — the exercise output goes into the PR description and a short note on issue #17/E01 evidence (per CONTRIBUTING: behavior, decisions, checks performed; no credentials/tokens).

**Interfaces:**
- Consumes: Task 4's documented commands only.

- [ ] **Step 1: Run the exercise in a disposable local stack** from repository root (Compose, fresh volume):

```sh
cp .env.example .env   # choose a local-only POSTGRES_PASSWORD; do not commit .env
docker compose up -d --build
# acceptance item 1
docker compose exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
# acceptance items 2–3 exercised per docs/foundation.md (browser: two sessions, 401s, 403 on incident mutation)
# acceptance item 4 spot-checks: unknown username and invalid usage — both must print one clean error line
docker compose exec api python -m cloudops.entrypoints.cli revoke-sessions ghost
docker compose down   # keeps records; remove network/volume if a fully disposable rerun is desired
```

Confirm: only documented commands were used, no manual SQL, and no password/token appears in any recorded output or log.

- [ ] **Step 2: Record evidence** — commit SHA, commands run, sanitized console output, environment (OS/Python/PostgreSQL), mapped one-to-one against the issue's five acceptance checkboxes; draft PR title/commits via `.agents/skills/contribution-writing/SKILL.md`; PR targets `main` from `feat/e01-account-lifecycle` with `Related to #17` (no closing keyword — E01 acceptance is separate).

- [ ] **Step 3: Cross-check the design** — final diff review against `docs/superpowers/specs/2026-10-25-account-lifecycle-design.md` §7: all five acceptance items demonstrably covered by Task 5 exercise (items 1–3 hands-on) plus tests (items 1–4), with transaction-boundary wording intact.

---

## Acceptance → task map

| Issue #17 acceptance item | Coverage |
|---|---|
| 1. Clean setup, both roles sign in; password recovery revokes prior sessions | Task 5 (hands-on) + Task 1 tests |
| 2. Two sessions, role-only change → 401s, unchanged password, 403 mutation | Task 5 (incl. UI check) + Task 1 tests (401/boundary; 403 denial is existing viewer behavior tested by `tests/test_access_admission.py`) |
| 3. Explicit revocation isolates the one account | Task 1 test 5 + Task 5 |
| 4. Unknown/invalid/rollback/concurrent-write cases consistent; boundary stated | Task 1 tests 2–3, Task 3 |
| 5. Isolated operator exercise, documented commands only, evidence in PR/E01 | Task 5 |
