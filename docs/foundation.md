# Foundation operation and contributor handoff

The local foundation stores application state and runs collection jobs. External evidence is synthetic. Scope and acceptance stay in [E00](https://github.com/pho-veteran/cloudops-copilot/issues/15) and the linked feature EPICs, rather than a second repository backlog.

## Delivered behavior and evidence

The integrated UI first landed in `47f09ff`; the verification/hardening wave in the same PR adds current contract checks, setup evidence and patched runtime dependencies. The PR and E00 record the final tested commit. These delivery notes include the completed F4 consumer verification; the maintainer directed that it be performed by the active implementer. No second contributor is required.

| Surface | Implemented behavior | Verification |
|---|---|---|
| Local setup | Compose web/API/worker/PostgreSQL; ordered migration and workload bootstrap; named persistent volume | Clean install and repeat migration/start checks |
| Access | Provision/reset CLI, scrypt passwords, server-side opaque cookie sessions, Origin/CSRF and backend Viewer denial | PostgreSQL API tests; Operator/Viewer browser sign-in |
| Admission | Atomic incident/run/job; actor-bound idempotency key; bounded pending queue; one active run per incident | Concurrent duplicate admission, queue-full and rollback tests |
| Collection | 30-second token lease, 5-second renewals, three attempts, atomic evidence/result publication | Real process kill and lease expiry; concurrent claims, stale/duplicate result rejection and failed publication rollback |
| UI | Queue, configured workloads, manual intake, selected run URL, immutable evidence and attempt history | Navigate/back/refresh and database/API/worker restart; read-only Viewer; light/dark and narrow layouts |
| Contract | Backend-owned OpenAPI, generated TypeScript, shared typed transport and feature-owned queries/fixtures | Drift detection with a deliberate extra route, synchronized regeneration, schema-checked typed fixture and consumer query test |

Browser walkthrough records use incident `6741962c-f88d-43fa-b994-712c4d0985c7`, run `b30f67b1-7be8-4054-9d3f-31e6946cdaa1` and evidence `e8906ed2-d797-430c-a93f-55271a45a409`. The IDs and payload hash were unchanged after restarting PostgreSQL/API/worker and refreshing the selected-run URL. Human-authored Vietnamese description was preserved. These records belong to a local development database; they are not cluster observations.

- [Operator after restart](evidence/foundation-operator.jpg)
- [Viewer inspecting the same persisted evidence](evidence/foundation-viewer.jpg)
- [320px Viewer layout](evidence/foundation-mobile.jpg)
- [Expired session preserving the selected incident/run route](evidence/foundation-expired-session.jpg)
- [Three failed attempts with no published evidence](evidence/foundation-failed-collection.jpg)

The browser also exercised an eight-second development session: polling returned 401 after expiry, protected content disappeared, sign-in retained the incident/run route, and no mutation was replayed. Default eight-hour session configuration was restored. Layout checks at 320px, 390px and 768px found no horizontal page overflow; light/dark views were inspected.

A clean archive of `abdcd65` was built in an isolated Compose project with an empty PostgreSQL 17.11 volume, using the README setup and only loopback port/origin overrides to avoid the running workspace. Account provisioning, same-origin web/API access, idempotent admission and worker evidence passed. Re-running migration and stopping/restarting the entire stack preserved session access, incident description and identical evidence. Viewer reads passed and both incident/run mutations returned 403. The isolated containers and disposable volume were removed afterward; no application data was cleared.

The failed-collection browser exercise created run `86719204-70a7-4497-a4b7-f1bc23bc1a85`, recorded three failed attempts, published no evidence and retained the earlier run. Ordinary worker configuration was restored afterward.

The images document local behavior. They do not prove live source collection, model quality, recovery safety or independent review.

## Configuration and account operations

Compose reads `.env`; see the committed `.env.example`. Use a URL-safe database password because the Compose connection URL interpolates it. Keep the exact loopback `WEB_ORIGIN` consistent with the browser URL. `COOKIE_SECURE=false` is an explicit HTTP-loopback exception; the default settings outside this file use Secure cookies. No CORS origin wildcard or public registration is enabled.

`config/development-workloads.json` contains at most 100 configured identities. Bootstrap validates the complete file before changing the active allowlist. Sync is transactional; removed rows become inactive rather than deleting incident history. A UID replacement cannot reuse the old persisted workload identity. The collector never reads cluster state or Secret contents.

```sh
# Create/reset an account, set its role, and revoke prior sessions.
docker compose exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
# Apply current migrations and synchronize configured identities.
docker compose run --rm migrate
```

Sessions expire absolutely after `SESSION_SECONDS` (default 28800, maximum 8 hours), without sliding renewal. Logout revokes the server session. The browser holds CSRF authority in memory, never localStorage; only theme preference and the separate simulation use localStorage. A 401 removes protected query data, closes pending dialogs and requires sign-in at the same internal route. Mutations are not retried or replayed automatically.

## Failure exercises

Run these only against the local development stack after creating an incident. Existing records should remain readable throughout worker failures.

```sh
# Stop collection; queued work remains saved. Restart to resume.
docker compose stop worker
docker compose up -d worker
# Force failed fixture collection for a new investigation.
COLLECTOR_FAIL=true docker compose up -d --no-deps --force-recreate worker
# Restore the ordinary deterministic collector.
docker compose up -d --no-deps --force-recreate worker
```

With forced failure, an investigation has at most three failed attempts, ends `failed` and has no published evidence. Restore the normal worker and explicitly start a new investigation; the failed run and earlier successful fixture snapshots stay readable.

For interruption, set `COLLECTOR_DELAY_SECONDS=60` on the worker with the same environment-override pattern, start a new run, then `docker compose kill -s SIGKILL worker` while it is collecting. Restore ordinary worker configuration. After the old 30-second lease expires, the next worker claims a new token. The old attempt becomes expired and cannot publish. The process interruption test reproduces this with a dedicated test database and a real elapsed lease, without modifying its deadline.

**Connections** observes worker heartbeats with a 45-second freshness threshold. A stale worker is separate from database readiness. API readiness requires the current Alembic head; worker unavailability does not make saved reads unavailable. A database outage returns an actionable API error. A missing/failed schema prevents startup readiness; follow README migration recovery instructions instead of stamping a revision or deleting data.

## Interfaces and module ownership

| Owner | Implementation boundary | Consumers |
|---|---|---|
| E00 | `shared/config.py`, `shared/database.py`, Alembic and contract harness; `lib/api` transport | All implemented feature modules |
| E01 | `features/access`: principal/session/permission and account provisioning | Every protected API; application shell |
| E02 | `features/workloads`: configured stable identity and active allowlist | Admission, collection identity and Workloads UI |
| E03 | `features/incidents`: manual admission coordinator and incident reads | Work queue and investigation workspace |
| E04 | `features/investigations` and shared job mechanics | Worker composition and run/evidence UI |
| E09 | `features/health`: classified observations | Connections UI |

HTTP handlers handle request/response/authentication; feature services own rules and transaction boundaries. Incident admission passes one SQLAlchemy session into E04, so partial incident/run/job admission cannot commit. Shared job code accepts feature handlers and never imports feature policy. Entrypoints register all persistence mappings before independent worker/CLI use. Network/synthetic collection work happens outside database transactions; fenced publication and job completion commit together.

Transport shapes live in feature Pydantic schemas, `contracts/openapi.json` and generated TypeScript. No hand-maintained DTO or second SDK is required. Only explicitly selected response fields reach the UI; password hashes, cookie tokens and job lease tokens are never response fields.

The implemented operation families are session/login/logout, workload inventory, manual incident create/list/detail, incident run create/list/detail and health observations. Use the generated artifact or the running [API documentation](http://localhost:8000/docs) for exact operations and field shapes.

- IDs are UUIDs; timestamps are UTC RFC3339. Synthetic evidence includes its schema version, source, workload UID, observed/collected time, payload and SHA-256 of canonical JSON (sorted keys, compact separators).
- List APIs use `limit` 1..100 (default 25), `offset` 0..100000 and `next_offset`. Incident/run order is creation time descending with UUID tie-break; workload order is UUID ascending. Concurrent inserts can shift offset pages; this foundation does not promise snapshot pagination.
- Operator mutations require exact Origin, session cookie, session-bound `X-CSRF-Token` and an `Idempotency-Key` UUID on admissions. The key is bound to actor, operation and canonical request. Identical replay returns the same IDs; a changed request with the same key returns 409. No client automatic mutation retry occurs.
- Errors use `{error: {code, message}}`: 401 for anonymous/expired session, 403 for Viewer/Origin/CSRF denial, 404 for missing or mismatched records, 409 for conflicting admission/active work/capacity, 422 for invalid fields/scope and 503 for database unavailability.
- Run states in this slice are `queued`, `collecting`, `insufficient_evidence`, `failed`. Successful synthetic collection ends `insufficient_evidence` with diagnosis/recovery unavailable, never completed RCA. Terminal runs are immutable; explicit retry creates a new run. There are no plan/execution or live collection endpoints.

## Available-operation authorization checks

The test-only inventory in `services/backend/tests/permission_inventory.py` classifies
the operations actually served by the application. `permission_cases.py` provides
feature-specific valid requests and success assertions; `test_permissions.py` runs
every classified operation with anonymous, expired, Viewer and Operator sessions.
Authentication, cookies, role dependencies and PostgreSQL persistence remain real;
these tests do not override the principal or permission dependencies.

| Classification | Current operation IDs | Required behavior |
|---|---|---|
| Protected reads | `get_session`, `list_workloads`, `list_incidents`, `get_incident`, `list_runs`, `get_run`, `get_health` | Viewer and Operator read existing workspace records; anonymous/expired requests return `401 session_expired` |
| Operator domain mutations | `create_incident`, `create_run` | Valid Viewer requests return `403 permission_denied`; Operators may admit work subject to feature rules |
| Session self-service | `logout` | Either authenticated role may sign out its own session with valid Origin/CSRF |
| Account entry | `login` | Valid credentials and configured Origin admit either account role; incorrect credentials return `401 invalid_credentials` |
| Public probes | `liveness`, `readiness` | Public status only; readiness returns sanitized `503 database_not_ready` when unavailable |

OpenAPI and generated docs (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`,
`/redoc`) are separate, explicitly public framework routes. Their actual GET/HEAD
definitions are inventoried; unknown routes do not inherit an exemption. A schema
describing a password input field is distinct from exposing a password value.

The inventory uses the pinned FastAPI route-context API to traverse included and
nested routers with their effective prefixes, including operations excluded from
OpenAPI. Each application operation needs an explicit unique ID, one method/path,
a classification and executable request/outcome fixtures. Missing/stale entries,
unknown classifications, duplicate IDs, unsupported mounts/WebSockets and
unclassified non-API routes fail the guard. The live OpenAPI document is compared
with schema-visible routes. Unit tests deliberately introduce these mistakes to
prove detection. Callable checks cannot judge assertion quality; reviewers must
inspect feature fixtures as well as inventory results.

For a new operation, its feature owner must:

1. Define its permission semantics in the owning EPIC using the existing principal.
2. Register its actual operation ID, method, path and classification in the inventory.
3. Supply valid existing-record/body/header fixtures and meaningful success assertions.
4. Run all four actor states through the real request path and assert domain-specific
   denial side effects. Do not use a missing record, invalid body or mocked principal
   as authorization evidence.
5. Regenerate transport artifacts if necessary, run checks and record the tested
   commit/environment and remaining integration gates in E01's evidence ledger.

Focused checks, from the repository root:

```sh
# No database or runtime credentials required:
uv run --project services/backend pytest -q services/backend/tests_unit/test_operation_inventory.py
# Requires a dedicated TEST_DATABASE_URL ending in _test; clears that database:
uv run --project services/backend pytest -q services/backend/tests/test_permissions.py
```

Permission fixtures create incidents through the API and complete the synthetic
development collector before testing another run admission. Denials compare fresh
committed snapshots of domain records, admissions, jobs, evidence, attempts and
account/session state. Tests inspect a persisted attempt lease sentinel to verify
it is absent from run responses. Password/session secrets are forbidden in response
bodies; the session's own CSRF field and evidence SHA-256 remain permitted outputs.
Existing workspace records are shared between application roles; this does not
introduce tenant or per-user record ownership. A run must still belong to its URL's
incident. Background worker processes must not target the disposable test database.

Future integration receipts remain with the feature owners:

| Owner | Operations requiring their own request-level evidence |
|---|---|
| E02 | Known-good baseline confirmation: Operator-only, with baseline validation and no change on denial |
| E03 | Notes and acknowledge/resolve/reopen: Operator-only, preserving lifecycle/state rules |
| E04 | Any new run controls: explicit semantics, run/job transition and denial evidence |
| E07 | Approval/rejection: Operator-only with current plan/state and fresh action; expired approval requires sign-in, plan refetch and no automatic replay |
| E08 | New report generation: Operator-only; Viewer may read/export existing permitted reports |
| E09 | New connection checks: Operator-only; Viewer may read existing permitted results |
| E01/E07/E10 | Maintenance recovery: trusted host authority and actual proof/provenance, never application role alone |

No absent operation is counted as verified. Current-route permission tests neither
close E01's future integration gates nor demonstrate a real HTTPS proxy, browser
SameSite behavior, cluster/model integration or deployment security.

## Focused checks

The Compose path is `./scripts/check-backend.sh`. It creates/reuses `cloudops_test`, applies migrations and clears that disposable database between tests. It never drops the normal application database.

For host backend tests against an already provisioned dedicated PostgreSQL database:

```sh
uv sync --project services/backend --frozen
# Set TEST_DATABASE_URL to your own PostgreSQL psycopg URL ending in /cloudops_test.
uv run --project services/backend pytest -q services/backend/tests_unit services/backend/tests
uv run --project services/backend ruff check --config services/backend/pyproject.toml services/backend scripts
uv run --project services/backend ruff format --check --config services/backend/pyproject.toml services/backend scripts
```

Tests reject a missing or non-test database URL before clearing tables. Do not use a production or shared database whose name merely happens to end in `_test`. The worker-interruption test makes the backend suite take at least 35 seconds.

```sh
npm ci --prefix tools/contracts
uv run --project services/backend python scripts/contracts.py
uv run --project services/backend python scripts/test-contract-drift.py
# After an intentional backend contract change:
uv run --project services/backend python scripts/contracts.py --write
npm --prefix apps/web test
npm --prefix apps/web run build
```

The drift exercise uses a temporary schema/consumer copy and deliberately adds a typed API operation. Unsynchronized artifacts fail; regenerated artifacts pass; the working tree is untouched. Export/check does not need runtime configuration, database access, cluster credentials or a worker.

## Consumer verification (F4)

At the maintainer's direction, the active implementer completed this exercise from a clean source archive of `a267c43`. This is self-verification and does not claim independent review. F4 no longer requires another human contributor.

The exercise implemented a read-only workload identity panel in `features/workloads`, using generated `WorkloadView` fields and the existing `useWorkloads` query/shared transport. Only `Workloads.tsx`, `WorkloadIdentity.tsx` and its focused test changed. The panel shows persisted record ID, cluster/namespace/Deployment UID, configured scope, fixture source and unavailable live health. Generated contracts, backend modules, shared infrastructure and other feature internals were unchanged.

Locked `npm ci` and the six workload consumer tests passed in the clean archive. Tests validate the typed fixture against OpenAPI, exercise the unchanged HTTP query, render Viewer/Operator behavior, retry an API failure without fabricated identity data, and preserve removed-scope identity. After integration, all 34 frontend tests, TypeScript/Vite build and the contract-drift check passed.

A separate Compose project with an empty PostgreSQL 17.11 volume started the same consumer against the real local API. Viewer and Operator sign-in both displayed persisted record `74483c3e-adc2-4d05-93a6-c218afc6ebe7`, with UID `development-checkout-v1`; these values matched authenticated API reads. The Viewer had no investigation mutation control, while the Operator retained the existing investigation action. Keyboard inspection worked. [Browser evidence](evidence/foundation-consumer.jpg) records the Viewer panel. The isolated stack used loopback port/origin overrides to avoid the existing workspace; its disposable database was removed after verification.

Reproduce the focused consumer checks with:

```sh
npm ci --prefix apps/web
npm --prefix apps/web test -- src/features/workloads
npm --prefix apps/web run build
```

Then follow README startup/account setup, open Workloads and choose **Inspect workload identity** with each role. The same query and component run against fixtures and the API without a second DTO or feature-policy edit. The implementation commit and final CI results are recorded in PR #16 and E00. No child issue is required for this single-PR delivery.

## Remaining operational boundaries

The final foundation polish separates deliberate sign-out from session expiry. An isolated
browser check signed in, opened Connections, signed out, confirmed the
[signed-out notice](evidence/foundation-signed-out.jpg), and signed back in to the same
Connections route. The [Connections view](evidence/foundation-connections.jpg) clarifies
that collector availability describes the configured synthetic adapter, not a successful
collection or live source health. Attempt history remains the source for collection
results; failed collection leaves `last_success_at` unset. All 35 frontend tests,
16 PostgreSQL tests, build, Python lint/format and contract/drift checks passed after
these changes.

Live Kubernetes/Prometheus packs, Alertmanager intake, notes/lifecycle/handoff, run comparison, runbooks, model analysis, plans/execution, KRR and deployment/recovery tooling remain in E01–E10 as applicable. No E00 fixture result satisfies those acceptance gates. The local worker cannot write to Kubernetes, and an application role does not grant maintenance/executor authority.

Fresh package audits are recorded with the tested PR commit. Existing web development-tool advisories remain an E10 release-tooling gate; a zero production-package audit is not a container/OS vulnerability scan or a complete security assessment. Only one maintainer has reviewed producer/consumer integration so far.
