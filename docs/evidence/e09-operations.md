# E09 C4 Operations evidence

The C4 handoff is `GET /api/connections/operations`, the generated `OperationsView`, and the
independently mountable `OperationsPanel` and `AlertReceipts` components. The API reports retained
active state at a database assessment time. Runnable age is the time since the oldest due queued
job was created. Current dispatch authority is a separate observation. Alert receipts describe
what happened at delivery time.

## C5 consumer handoff

- Mount `OperationsPanel` and `AlertReceipts` from `apps/web/src/features/connections` inside the authenticated page. Both components use the shared transport and need no role prop.
- Import `operationsFixture` and `alertReceiptsPageFixture` from the feature's `fixtures.ts`. Both return fresh, generated-contract fixtures with matching capacity Run and receipt IDs. Each accepts response field overrides for failure recovery, empty data or sample truncation tests.
- `due_retry` is a subset of `runnable`. Due verification slots also count toward `runnable`. The recovery verification row exposes `verification_slots` for scheduled observations, including the earliest future slot. Dispatch can be paused for a job kind with runnable work.
- `jobs_capped` identifies overflow of the active-job read, which withholds all counts and runnable age. Missing attempt history for an eligible queued verification job withholds counts for that job kind. A null `expired_lease` within otherwise known counts means a running job lacked a lease deadline. Use the separate `has_more` fields to label the two bounded Run samples.

## PostgreSQL projection

An isolated PostgreSQL 17.11 database was seeded with 30,000 terminal jobs, Runs and analyses,
1,050 queued jobs, 50 capacity-deferred Runs and 50 quota-waiting Runs. With production caps
and analyzed tables, `EXPLAIN (ANALYZE, COSTS OFF)` showed:

| Query | Access path | Rows at limit |
|---|---|---:|
| Active jobs | `ix_jobs_state` on queued, running and held | 1,001 |
| Capacity Runs | `ix_runs_deferred`, primary-key incident lookup and a `jobs_kind_subject_id_key` anti-join | 21 |
| Quota Runs | `ix_runs_quota_wait`, primary-key incident lookup and an indexed analysis lookup per Run | 21 |

The projection withholds job counts and queue age on overflow and returns up to 20 Runs of
each type with a separate truncation flag. The focused route test repeats this with small caps
and checks the selected columns, stable time/ID order and read-only SQL. Migration 0018 built
the quota index on this fixture, was downgraded to 0017, and reapplied successfully.

## Repeatable focused checks

The C4 worktree based on `43eae2b` passed 98 backend cases across operations, intake, diagnosis
lifecycle, jobs and dispatch on Linux with PostgreSQL 17.11. A separate check passed all 28
operation-inventory and Operations permission cases. The full web run passed 288 tests and
built successfully on Linux with Node 24.21.0. Contract generation and drift checks, Ruff,
and Alembic metadata checks passed.

The full backend run finished with 2,224 passed, 1 skipped and 10 failed. Eight E06 driver
cases needed Git, which was absent from the test image. The entire nine-case driver passed
when rerun from the Git worktree. The other two failures came from the matrix expecting an
empty capacity sample even though its shared fixture includes a deferred Run. The updated
matrix checks that exact Incident and Run and passed in the 28-case rerun above.

Point `TEST_DATABASE_URL` to an isolated PostgreSQL database whose name ends in `_test`.

```sh
uv run --project services/backend pytest -q services/backend/tests/test_connection_operations.py
uv run --project services/backend pytest -q services/backend/tests/test_alert_intake.py -k 'permanent_identity_refusals or capacity_defers_refuses_replays'
uv run --project services/backend pytest -q services/backend/tests/test_diagnosis_lifecycle.py -k quota_hold_resume
uv run --project services/backend pytest -q services/backend/tests_unit/test_operation_inventory.py
uv run --project services/backend pytest -q services/backend/tests/test_permissions.py -k read_connection_operations
npm --prefix apps/web test -- --maxWorkers=1 src/features/connections/OperationsPanel.test.tsx src/features/connections/AlertReceipts.test.tsx src/app/routes.test.ts
```

The intake tests exercise authenticated rejects and capacity deferral through the intake
API with a controlled scoped mapping adapter. Freeing capacity preserves the reason until the
worker promotes the same Run. The original delivery receipt survives promotion. The diagnosis
test uses a controlled HTTP 429 to persist a quota wait and checks that crossing the retry time
leaves it visible until resume. Session tests cover Operator, Viewer, anonymous and expiry.
The repository permission inventory classifies the Operations route as a protected read. The
role matrix verifies the response against its schema and compares persisted domain and session
rows before and after every request.
Component tests verify the exact incident router targets, actionable rejection copy, capped
and empty states, and preservation of the last snapshot after a failed refresh.

## Installed API and browser preview

A disposable Microsoft Edge 157 profile opened a temporary Connections preview against the
installed API and a separate migrated PostgreSQL 17.11 database. The database held synthetic
receipt, Run and job fixtures. The preview mounted both C4 panels and used the existing persisted
router. The browser relay to the existing Edge session was unavailable.

| Role and viewport | Keyboard navigation | Panel overflow |
|---|---|---:|
| Viewer, 320px light | Capacity Run and linked intake receipt opened the same saved Run | 0px |
| Operator, 390px dark | Quota Run opened its saved Run, and the intake receipt opened the capacity Run | 0px |

The capacity incident also held a second historical Run. The browser received HTTP 200 for the
exact Run on each keyboard navigation. The rejected receipt showed scope guidance and had no
link. Each role saw the dispatch pause, runnable backlog, future retry and expired lease together.
A controlled HTTP 503 on refresh preserved the last snapshot, and Retry recovered it. Browser
API traffic after sign-in consisted of GETs. The installed C5/C6 page remains a separate
integration check.

## Verification slots, dispatch authority and retained receipts

The follow-up to `7a6949b` passed 129 focused backend cases in the Linux image with
PostgreSQL 17.11. They cover operations, intake, job inventory, dispatch permits and worker
admission, diagnosis lifecycle and route permissions. The Connections and route tests passed
all 71 cases. The production web build, Ruff check and format, and contract generation and
drift checks passed. The code and test diff from `7a6949b` has SHA-256
`e4ad62dc24a85d7d1d997dc18e947077edd12fd153daa24934124a2760bbb71f`.

The revised active-jobs query was profiled independently with 30,000 terminal jobs and 1,050
queued jobs. The read stopped at 1,001 rows using `ix_jobs_state`. The attempt lookup used
`ix_job_attempts_job_id` for the 500 started, queued verification jobs in the bounded sample.

In a separate migrated PostgreSQL lab, a disposable Microsoft Edge 157 profile opened the
C4 preview as Viewer at 320px in light mode and Operator at 390px in dark mode. Both roles
saw a due verification slot and a future verification slot alongside a single due retry. A
valid permit file with its lock removed showed paused dispatch. The retained retry receipt
described the earlier delivery and exposed no link. The later receipt opened the exact saved
Run by keyboard. A controlled HTTP 503 preserved the last Operations snapshot and Retry
recovered it. Removing the session cookie returned HTTP 401 and brought the browser back to
sign-in. All API requests after sign-in were GETs. Neither panel overflowed horizontally.
