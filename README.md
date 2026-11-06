# CloudOps Copilot

Kubernetes incident investigation and bounded recovery for a small operations team.

**Current implementation:** a local React/FastAPI/PostgreSQL application with server-side sessions, Operator/Viewer permissions and durable jobs. Operators manage incidents, baseline metadata, scoped connection checks, KRR jobs and exact-plan decisions; Viewers inspect and export stored records. Investigation Runs persist source-aware metadata, coverage and bounded retained payloads; the only installed investigation adapter is an explicitly enabled **synthetic development fixture**. Known uninstalled Kubernetes/Prometheus slots remain unavailable. The optional [E01 integration lab](docs/e01-local-lab.md) adds separate real K3s observations, a conditional scale executor and trusted maintenance. Synthetic evidence never authorizes a cluster write. AI diagnosis, qualified resource recommendations and automatic remediation verification remain unavailable.

Active scope and feature acceptance live in the [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) and [Foundation E00](https://github.com/pho-veteran/cloudops-copilot/issues/15). This implementation does not complete the full operational release. The foundation consumer integration exercise is self-verified in PR #16; it does not require a second contributor.

The optional [workload observer](docs/workload-observation.md) adds persisted UID-owned Kubernetes rollout health, freshness, Pod/restart context and exact related incidents. [Versioned healthy baselines](docs/workload-baselines.md) retain two fresh observations and feed an exact, read-only recovery context. [Integrated local verification](docs/e02-local-acceptance.md) keeps API receipts and the trusted-browser gate explicit. Application traffic health and restore execution remain unavailable.

## Quick start

Prerequisites: Docker Engine with Docker Compose v2.20 or newer, Git, and available loopback ports 5173 and 8000. The container path requires no host Python/Node, paid account, model key or cluster credentials. Images pin Python 3.13.11, Node 24.21.0 and PostgreSQL 17.11; Python and npm dependency lockfiles are committed.

From the repository root:

```sh
cp .env.example .env
# Edit .env: choose a local POSTGRES_PASSWORD containing URL-safe letters/digits/hyphens.
docker compose up -d --build
# Issue the fresh-install job dispatch permit once (trusted host step; see docs/job-dispatch.md).
docker compose run --rm dispatch-admin bootstrap --fresh-install --reason "Local fresh install" --evidence "README quick start"
# Provision accounts; each command prompts for a password (12..256 characters).
docker compose exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
```

Open [http://localhost:5173](http://localhost:5173). Use that exact origin: login and mutations validate `WEB_ORIGIN`. The default configuration explicitly permits non-Secure cookies only on loopback HTTP. Keep the local services on loopback. A separate [local HTTPS exercise](docs/foundation.md#local-https-exercise) serves the compiled web build and API through one proxy; the [K3s integration overlay](docs/e01-local-lab.md) supplies scoped lab credentials without changing the host kubeconfig. Release deployment remains E10 work.

`WEB_ORIGIN` must use the browser's exact origin spelling, with no trailing slash, credentials, path, query, fragment or explicit default port. HTTP requires exactly `localhost` or `127.0.0.1` and `COOKIE_SECURE=false`; HTTPS requires `COOKIE_SECURE=true`. Contradictory configurations fail at startup. See the [browser session configuration and HTTPS handoff](docs/foundation.md#browser-session-configuration-and-https-handoff) for accepted examples and the remaining E10 proxy checks.

`migrate` runs Alembic and synchronizes `config/development-workloads.json` before API/worker start. PostgreSQL uses a named volume. No accounts or default passwords are seeded. Until the dispatch permit exists, the worker claims nothing and queued Runs explain the pause; the permit lives in its own `job_dispatch` volume, outside PostgreSQL. Provisioning an existing username resets its password/role and revokes its sessions. To change only the role or revoke all sessions without touching the stored password, use `account-role <username> <viewer|operator>` or `revoke-sessions <username>`. For automation, `account ... --password-stdin` reads one password from stdin; keep it out of command arguments and source control.

## First interactive journey

1. Sign in as the Operator. Open **Workloads** and choose **Investigate workload** on `demo/checkout`.
2. Enter a title, optional description, an investigation question and a time window (last 15/30/60 minutes or a custom interval of up to 60 minutes), then **Create incident and collect evidence**. Incident, run and job admission are one database transaction; the server fixes the absolute UTC window when it accepts the request.
3. Observe queued/collecting states, then inspect **Evidence**, five-pack coverage and **Collection attempts**. Completed collection, including an all-unavailable result, ends **insufficient evidence** / `analysis_unavailable`; this is not completed RCA. The optional development fixture never fills live source gaps.
4. Select a source to inspect its exact metadata and lazily loaded content, with explicit unavailable, legacy or expired states. The URL retains `?run=<UUID>&evidence=<UUID>` through refresh/back; selecting a source moves keyboard focus to the viewer. Inspect the original UID/window, versions, SHA-256, pin and expiry. See [source-aware evidence and retention](docs/foundation.md#source-aware-evidence-and-retention).
5. Stop/start the stack with `docker compose down` and `docker compose up -d`, then refresh. IDs, human-authored description, evidence and attempts persist. **Request a new investigation** asks for a reason and window, shows a review step, and creates another run while preserving earlier snapshots. A queued run can be cancelled before collection starts. See [investigation requests and bounded admission](docs/incident-admission.md).
6. Add a note and acknowledge the incident. Preview **Current handoff**, copy or download the loaded Markdown, then resolve with a reason. **View saved resolution** opens the frozen report; reopening or requesting a later Run does not change its bytes. Activity history includes actor/time and **Load older activity**. See [stored-data handoff](docs/foundation.md#stored-data-incident-handoff).
7. Sign out and sign in as the Viewer. The same records and handoff remain readable; incident/run mutations are unavailable in the UI and denied by the backend.
8. Open **Connections** for database and worker heartbeat observations. An unavailable model or executor is shown explicitly. **Alert intake** lists authenticated Alertmanager delivery outcomes; intake stays disabled until configured as described in [Alertmanager intake](docs/alert-intake.md).

See [foundation operation and contributor handoff](docs/foundation.md) for failure/restart exercises, API conventions, verification evidence and the consumer integration verification.

## Start, stop and recover locally

```sh
docker compose ps
docker compose logs --tail 100 api worker migrate
docker compose down                  # stops services; preserves the database volume
docker compose up -d --build         # reapplies migrations/config sync; preserves records
docker compose run --rm migrate      # explicit migration and configured-workload sync
```

After editing workload configuration or backend source, rebuild/start with `docker compose up -d --build`. Removed workload identities remain in incident history but cannot admit new work. Changing a Deployment UID creates a different persisted identity.

If `migrate` fails, API/worker do not start ready. Inspect `docker compose logs migrate`, correct configuration or the migration failure, then rerun the explicit migration command and `docker compose up -d`. Do not mark a revision as applied or delete the database to bypass a failed migration. Migration `0003_e01_consumers` preserves prior accounts/workloads/incidents and labels prior workloads as development fixtures. It adds consumer/recovery tables and expands incident states. Downgrade destroys new feature data and does not make expanded lifecycle values compatible with an older app. For the integration lab, use the [quarantined backup/restore procedure](docs/e01-local-lab.md#restore-and-restart), revoke restored browser sessions through the CLI, and establish fresh writer proof before enabling targets.

If the worker stops during collection, a restarted worker reclaims the job after its 30-second lease. Heartbeats renew the lease every 5 seconds. At most three attempts run; older tokens cannot publish. Failed collection publishes no successful evidence. The Operator can explicitly create a new investigation after failure. Reads may be repeated; no exactly-once external call guarantee is made.

The evidence transport is intentionally breaking: Run list/detail no longer embed payloads. Upgrade API, worker and web together using the [evidence migration procedure](docs/foundation.md#source-aware-evidence-and-retention); old evidence IDs/digests and saved report bytes remain unchanged. Unpinned content expires after 30 days, while saved resolutions and the last useful unresolved-incident collection retain independent pins. Payload purge is a trusted dry-run/apply CLI operation, not a browser control.

## Contributor setup and checks

Read [CONTRIBUTING.md](CONTRIBUTING.md). For host development, install Node from `.node-version`, Python from `.python-version` and uv 0.9.26. Keep API/worker/database in Compose and run web locally:

```sh
docker compose stop web
npm ci --prefix apps/web
npm --prefix apps/web run dev
```

Use `http://localhost:5173` even if Vite prints `127.0.0.1`; Origin must match configuration. The default Vite proxy uses the loopback API at port 8000. To return web to Compose, stop the host Vite process, then run `docker compose up -d web`.

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm --prefix apps/web run build
# Configuration-only tests need no database.
uv run --project services/backend pytest -q services/backend/tests_unit
./scripts/check-backend.sh
# Contract tooling is isolated because its TypeScript peer range differs from the app's.
npm ci --prefix tools/contracts
uv sync --project services/backend --frozen
uv run --project services/backend python scripts/contracts.py
uv run --project services/backend python scripts/test-contract-drift.py
```

The backend check creates/reuses **cloudops_test**, a separate disposable database, and clears only its application tables. Do not point tests at application data. Host tests require `TEST_DATABASE_URL` ending in `_test`; see the handoff guide. Contract export/check requires no database, running worker, runtime configuration or external credentials. After changing Pydantic routes/models, regenerate with `uv run --project services/backend python scripts/contracts.py --write`, review both generated artifacts, and run producer/consumer checks.

CI runs web tests/build, PostgreSQL authorization/admission/migration/worker checks and deliberate contract-drift rejection. These checks establish local application behavior and fixture contracts, not live cluster/model behavior or release acceptance.

## Fixture simulation

Open [the explicitly labeled simulation](http://localhost:5173/?simulation=1) to explore the broader 24-story experience. Choose **OOM recovery**, inspect evidence, review/approve the simulated plan and use **Advance simulation**. **Change story** and **Restart story** reset fixtures. Its state stays in localStorage and makes no backend, Kubernetes or model calls. The persisted application never uses simulation controls or fixture RCA as real results.

## Feature roadmap

| EPIC | Feature |
|---|---|
| [E00 · #15](https://github.com/pho-veteran/cloudops-copilot/issues/15) | Interactive application foundation v0.1 |
| [E01 · #4](https://github.com/pho-veteran/cloudops-copilot/issues/4) | User access and application sessions |
| [E02 · #5](https://github.com/pho-veteran/cloudops-copilot/issues/5) | Scoped workloads, health and known-good baselines |
| [E03 · #6](https://github.com/pho-veteran/cloudops-copilot/issues/6) | Incident intake, work queue, lifecycle and handoff |
| [E04 · #7](https://github.com/pho-veteran/cloudops-copilot/issues/7) | Evidence collection, durable investigation runs and comparison |
| [E05 · #8](https://github.com/pho-veteran/cloudops-copilot/issues/8) | Versioned runbooks and knowledge retrieval |
| [E06 · #9](https://github.com/pho-veteran/cloudops-copilot/issues/9) | AI diagnosis through OpenAI-compatible endpoints |
| [E07 · #10](https://github.com/pho-veteran/cloudops-copilot/issues/10) | Approved recovery, verification, reconciliation and rollback |
| [E08 · #11](https://github.com/pho-veteran/cloudops-copilot/issues/11) | Resource recommendations, coverage and export |
| [E09 · #12](https://github.com/pho-veteran/cloudops-copilot/issues/12) | Connections, freshness and operating visibility |
| [E10 · #13](https://github.com/pho-veteran/cloudops-copilot/issues/13) | Reproducible lab, evaluation and release delivery |
