# Design — Manage account roles and revoke sessions through the maintenance CLI

**Issue:** [#17 — [E01] Manage account roles and revoke sessions through the maintenance CLI](https://github.com/pho-veteran/cloudops-copilot/issues/17) · **Parent:** [E01 #4 — User access and application sessions](https://github.com/pho-veteran/cloudops-copilot/issues/4)
**Date:** 2026-10-25 · **Status:** Proposed for implementation

---

## 1. Intent and why

A trusted deployment operator must be able to (a) change an application account's role and (b) revoke all of that account's sessions — without resetting the account's password, editing database rows, or interrupting other users. Today the maintenance CLI couples every role change to a new password entry. Password provisioning/recovery already shipped in PR #16; this issue **extends that existing path** rather than rebuilding it.

## 2. Current state (will be reused, not rebuilt)

- `services/backend/src/cloudops/features/access/service.py` (~90 lines):
  - scrypt password hashing, `password_hash` enforces 12..256 characters;
  - opaque server-side sessions (`LoginSession` rows keyed by SHA-256 token hash, absolute expiry);
  - `login()` locks the `User` row `with_for_update()`;
  - `authenticate()` reads the sessions table **directly on every request** — deleting committed session rows therefore produces `401 session_expired` for later requests; nothing else is cached;
  - `provision()` upserts a user: sets password+role and deletes all of that user's sessions in one transaction. Requires a password parameter — this coupling is the gap.
- `services/backend/src/cloudops/entrypoints/cli.py`: one subcommand, `account <username> --role viewer|operator [--password-stdin]` (prompts via `getpass` or reads stdin). Domain errors currently surface as tracebacks.
- Models (`access/models.py`): `User(id UUID PK, username unique, password_hash, role CHECK IN ('viewer','operator'))`; `LoginSession(token_hash PK, user_id FK, csrf_token, expires_at)`. No new account status or migration is planned. (Note: `sessions.user_id` is already indexed, so bulk revocation deletes need no schema change.)
- Docs to update: `docs/foundation.md` § "Configuration and account operations".

## 3. Goals

1. **Explicit, compatible commands.** Keep `account <username> --role ...` provisioning/reset behavior exactly as documented (hidden password entry, `--password-stdin`). Add two explicit commands — role-only change and all-sessions revocation — and publish their final syntax in the operator instructions.
2. **Atomic role change + revocation.** A role-only change preserves the password hash and account UUID and commits the role change together with revocation of that user's sessions in a single transaction.
3. **Explicit revocation preserves identity and credentials.** Revoking sessions changes neither the password hash, the role, nor the UUID; it only deletes that account's sessions.
4. **Clean failure.** Unknown accounts or invalid roles fail clearly, create no account, and leave no partially changed data.
5. **Preserved revocation boundary.** Same-account login/recovery/role operations serialize as needed so the chosen revocation boundary holds. No promise of cancelling already admitted jobs or sessions authenticated before the revocation committed — tests state the transaction boundary rather than claiming retroactive cancellation.
6. **Evidence.** Focused PostgreSQL tests and actual CLI tests, updated account recovery/role instructions, and an isolated operator exercise tied to the PR/E01 evidence.

## 4. Non-goals and anti-goals (from the issue)

- No password reset inside the new commands; no public signup; no account-admin UI; no third role; no new identity provider.
- No redesign of HTTP cookies, frontend routing, feature authorization, or E07 lock release. No migration or new account status unless implementation evidence demonstrates a need.
- **Anti-goal:** implying retroactive cancellation of requests authenticated before the revocation committed.
- Role-only changes stay write-side only; the operation/role matrix (issue #19) is not touched here.

## 5. Decided

These are settled; implementation does not revisit them.

### D1 — Command surface (syntax is this design's call)

```sh
# Existing (unchanged): create account, or reset it with a NEW password; revokes sessions.
account <username> --role <viewer|operator> [--password-stdin]

# NEW: role-only change. Never prompts for or accepts a password.
# Preserves password hash + UUID. Commits role change + session revocation atomically.
account-role <username> <viewer|operator>

# NEW: explicit kick. Deletes all sessions of <username>.
# Preserves password, role and UUID. No secret read.
revoke-sessions <username>
```

Flat argparse style matches `bootstrap` / `sync-workloads`. A same-role reissue still commits and revokes (no special-casing). The existing documented usage `python -m cloudops.entrypoints.cli account operator --role operator` stays valid.

### D2 — Service layer

Two new functions in `access/service.py`, each opening one `db.begin()` transaction and locking the `User` row `with_for_update()` — the same pattern as `login()`/`provision()`, which is what serializes same-account login/reset/role/revoke:

- `set_role(db, username, role)` — validates the role, updates `role` only, deletes that user's `LoginSession` rows, commits. Unknown username → clear domain error; invalid role → clear domain error; no user row is ever created.
- `revoke_sessions(db, username)` — deletes that user's `LoginSession` rows only. Unknown username → clear domain error.
- Neither function takes a password parameter.

### D3 — CLI error surface (this design's call)

`main()` catches `DomainError`/`ValueError`, prints a one-line `error: <message>` to stderr and exits nonzero — a clean operator failure instead of a traceback. Success output remains one short line (wording left to the builder). No passwords or tokens are ever printed.

Builder-verified notes, accepted: pydantic v2's `ValidationError` subclasses `ValueError`, so the same catch also turns today's bad-password/username traceback into a clean line; wrapping the whole dispatch means `sync-workloads` failures become one-liners too. Argparse's own `SystemExit` is unaffected.

### D4 — Revocation semantics (issue-mandated)

Because `authenticate()` reads the sessions table per request, committed deletion means later authenticating requests get `401 session_expired`. Nothing is claimed about requests authenticated before the commit; in-flight/already-admitted work is out of scope.

### D5 — Tests (issue mandates coverage; file and mechanics as follows)

New `services/backend/tests/test_account_lifecycle.py`, running against the existing session-scoped disposable `_test` database fixtures in `tests/conftest.py`:

- **Service-level (PostgreSQL):** role-only change preserves password hash + UUID while revoking sessions; explicit revocation preserves password + role + UUID; unknown account and invalid role leave zero rows changed; rollback/concurrent-login cases hold the account/session state consistent.
- **Actual CLI level:** invoke `cloudops.entrypoints.cli.main()` in-process with patched `sys.argv` (and stdin stubs where inputs are needed) against the test database, assert exit behavior, output and side effects — including that an unknown account returns a clean failure, not a traceback.
- Tests assert the transaction boundary wording; they do not claim cancellation of pre-commit authenticated requests.

### D6 — Documentation

`docs/foundation.md` § "Configuration and account operations" publishes the final three-command syntax, stating explicitly which operations preserve the password. README's quick-start account notes gain one short sentence pointing at the new role-only/revocation commands. The isolated operator exercise (acceptance item 5) uses only these documented commands.

### D7 — Generated contracts

No HTTP route, Pydantic response/request schema, or transport change is planned, so no OpenAPI/TypeScript regeneration is expected. If the builder's implementation turns out to touch shared schemas, regenerate with `scripts/contracts.py --write` per CONTRIBUTING (do not hand-edit generated artifacts).

## 6. Open areas (within the boundaries above)

- Exact domain-error codes/messages for "unknown account" and CLI wording — pick clear strings; do not invent a transport-level contract change.
- Concrete test mechanism for the concurrent-login/serialization case — e.g. interleaved sessions across two connections; any mechanism is fine as long as it exercises real locking and real commit order and does not fake the boundary.
- The isolated operator exercise script/steps for PR evidence.

## 7. Acceptance (all five must pass, from the issue)

1. A clean setup provisions Viewer and Operator accounts; both sign in. Password recovery through the documented command revokes prior sessions; the old password is rejected and the replacement works.
2. With two active sessions for one Operator, a role-only change to Viewer commits and both old sessions receive 401 on requests authenticating afterward. The unchanged password signs in as Viewer; an incident/run mutation returns 403.
3. Explicit session revocation invalidates all sessions for that account without changing its password, role or ID; another user's active session remains usable.
4. Unknown-account, invalid-input and rollback/concurrent-login cases leave a consistent account/session state. Tests state the transaction boundary rather than claiming retroactive cancellation.
5. An isolated operator exercise uses only documented commands, with no database edits or password/token output in logs. Commit, commands, sanitized results and environment are recorded in the PR/E01 evidence.

## 8. Verification

- `./scripts/check-backend.sh` (lint, format, PostgreSQL tests — always against a `_test` database, never application data).
- Frontend and web tests are not expected to change; CI regenerates nothing unless D7's exception triggers.
