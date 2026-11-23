# E10 S1 release package acceptance (#120)

This ledger records the S1 acceptance of the pinned Compose and HTTPS package for [#120](https://github.com/pho-veteran/cloudops-copilot/issues/120) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13). The package tooling is from [PR #127](https://github.com/pho-veteran/cloudops-copilot/pull/127).

All runs used a local Docker Desktop 29.9.2 Linux engine (amd64, cgroup v2, 8 CPU, 10.7 GiB) with a WSL2 Ubuntu 24.04 client, on 2026-11-23. This is local, owned evidence. It is not an EC2 install, a cloud measurement or a browser exercise. Those gates belong to [#121](https://github.com/pho-veteran/cloudops-copilot/issues/121) and [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).

## Identity

| Item | Value |
| --- | --- |
| Candidate revision (B2) | `57ccfd61ffbe8170918defcd7f408e3aa298b386` |
| Previous release revision (B1) | `86649c0faec1bf8b5dde7fe1e16b371ef2967a1f` (main before this change) |
| B2 `source.tar` / `images.tar` SHA-256 | `build.json` → `bundles[0].manifest.sha256` |
| B2 backend / https / db image IDs | `sha256:cf17184e…789a9` / `sha256:c205835c…3c05d` / `sha256:665dc7b3…5e994` |
| Upstream bases (resolved digests) | `python:3.13.11-slim-bookworm`, `ghcr.io/astral-sh/uv:0.9.26`, `node:24.21.0-bookworm-slim`, `caddy:2.10.2-alpine`, `postgres:17.11-bookworm`, digests in each manifest |
| Candidate CI | [Repository checks run 45122691660](https://github.com/pho-veteran/cloudops-copilot/actions/runs/45122691660): web tests and build, backend, migrations, contract drift and HTTPS smoke passed |
| Linux backend suite on the lock change | 2289 passed |

Commits after the candidate add only this evidence directory.

## Acceptance

| #120 requirement | Result | Receipt |
| --- | --- | --- |
| Clean isolated host/project starts pinned API, worker, web and DB | Release tags were removed before load. B1 was then installed into a project with no prior containers or volumes, from an install directory holding only the extracted `source.tar`, with `--no-build --pull never`. `verify` and `verify --loaded` passed. Every container ran its manifest image ID. B2 was installed the same way into a separate disposable project. | `install.json` |
| Migrations apply with repeatability | A fresh database reached head `0020`. A same-core upgrade and a repeated start applied nothing further. A schema-crossing upgrade advanced `0017` → `0020`. | `install.json`, `upgrade.json` |
| Same-origin trusted HTTPS on compiled web | `scripts/smoke-https.py` passed against B1, B2 fresh, U1 and U2. It validated the chain and hostname with the project CA and checked the compiled `/assets/` page, Origin and CSRF denials, Secure, HttpOnly, SameSite=Lax, host-only cookies, Viewer denials, logout and token replay. The only published listener was `127.0.0.1:8443`. | `install.json`, `upgrade.json` |
| Loads scoped components from external credential references | In the release lab, a live collection through the scoped reader stored Kubernetes state, events and current/previous logs with content digests. Images contain no credential files. Credentials live in the private `0700`/`0600` lab directory. | `separation.json` |
| Populated upgrade preserves records | U1 (B1 → B2): 42 of 45 tables were identical. U2 (source `f74d3ed` → B2): 41 of 45 tables were identical. The remaining differences are the scope synchronization counters (`observation_epoch`, `applied_sequence`, `latest_sequence`), the live worker heartbeat and, in U2, `alembic_version`. Each column difference was checked against the pre-upgrade backup. The HTTPS CA was unchanged. | `upgrade.json` |
| Generated contracts match backend models | `create_app().openapi()` in the B2 backend image, run without a network, is byte-identical to `contracts/openapi.json` in the B2 source (`7d2b7076…`). TypeScript drift passed in candidate CI. | `upgrade.json` → `contract` |
| Writer credential and executor socket separated from API/worker | The lab ran only bundle images (no local build). The API and worker have no writer credential or maintenance socket, and the API has no dispatch key. No container mounts the Docker socket. Real K3s denied reader patches, reader/writer Secret reads and writer patches outside the named target (403). The executor started in recovery mode with its target disabled. | `separation.json` |
| Sanitized configuration, logs and artifacts | Container logs of every release project and all raw and final receipts were scanned against the actual private passwords, the database password, the reader/writer tokens and the dispatch, KRR and intake keys, plus bearer, JWT, cookie and URL-credential patterns. No hits. | `resources.json` → `sanitization` |
| Reproducible build metadata, defined architecture/OS | Two B2 builds were compared, one on the default builder and one on an empty-cache `docker-container` builder. Revision, upstream digests, `source.tar`, lock files, Python package set, backend application tree, compiled web tree and Caddyfile were equal. Image IDs differ by build metadata and are bound by each manifest. Bit-for-bit reproducibility is not claimed. | `build.json` |
| Port, storage and image requirements, deliberate stop | Bundle is about 304 MB (images 282 MB, source 22 MB). Loaded images: backend 385 MB, https 84 MB, db 620 MB. The core database volume was about 67 MB after these runs. Idle core uses about 217 MiB in total. Startup takes about 16 s. `compose stop` kept all four volumes, and a restart preserved data, CA and the dispatch permit. | `resources.json`, [requirements](../../release-package.md#requirements) |
| Default writer/dispatch safe, readiness separate from worker | Dispatch starts `paused/permit_missing` until the trusted `dispatch-admin bootstrap --fresh-install`. Upgrade pauses dispatch with `enter-recovery` until an explicit `exit-recovery`. With the worker stopped past its 45 s bound, readiness stayed 200 and health reported `worker: stale`. | `install.json`, `upgrade.json` |

## Dependency advisories

Details are in `advisories.json`.

- **Web npm:**
  - The baseline had 7 findings (1 high `body-parser`, 6 moderate), all in the `shadcn` CLI development chain.
  - A lock-only `npm audit fix` cleared `body-parser`, `hono` and `@hono/node-server`.
  - Four moderate findings remain from the `minimatch` → `ts-morph` → `shadcn` chain. The only fix is a breaking `shadcn` downgrade, so they are accepted as development-only. Production audit is 0, and the compiled `/srv` output contains none of these modules.
  - The historical E00 set of seven high development-tool advisories no longer applies.
- **Contract tooling npm:** 0.
- **Backend Python:** `pip-audit` found `opentelemetry-api` 1.45.1 (CVE-2026-126360 / GHSA-mf5v-w3w3-f6cg). The lock now pins 1.45.2, and the candidate has 0 findings.
- **Runtime image OS and Go packages (Docker Scout, critical/high):**
  - backend: 8 critical / 52 high, Debian bookworm.
  - https: 29 critical / 109 high, Caddy 2.10.2 Go modules and Alpine.
  - db: 4 critical / 50 high, Debian and the gosu Go stdlib.
  - Most findings have fixed versions in newer base images.
  - **This remains an unresolved release-gate item for E10 #13.** Remediation means refreshing the pinned bases (a newer Python 3.13 slim patch, Caddy 2.11.x and a newer PostgreSQL 17 patch). That refresh changes runtime, CI and lab images, so the S1 proof must be repeated after it.
  - Exposure in this candidate is the loopback HTTPS origin and the private Compose network.

## Handoff to #121 and #124

- **Install inputs:**
  - Build on a trusted Linux/amd64 host with `scripts/release-bundle.py build`.
  - Transfer the bundle and verify it before extraction, then `verify --loaded`.
  - Start with the documented core `compose` function (`compose.yaml`, `compose.https.yaml`, `compose.release.yaml`) and a private `host.env`.
  - Provision Operator and Viewer through the CLI.
  - Copy the project CA for checks.
- **Owned resources per core project:** volumes `database`, `job_dispatch`, `local_https_data` and `local_https_config`. Published listener `127.0.0.1:8443`. Three images named `cloudops-release-{backend,https,db}:<revision>-<id>`.
- **Upgrade and restore order:**
  1. Verify and load the new bundle.
  2. Take a `pg_dump` backup.
  3. Run `enter-recovery`.
  4. Stop the worker, then the API and HTTPS.
  5. Start the new bundle on the same project.
  6. Check readiness, smoke and records.
  7. Run `exit-recovery`.
- **Scoped lab topology:** [scoped lab with release images](../../release-package.md#scoped-lab-with-release-images). The executor and every target stay disabled until the E07 maintenance proof.
- **Version note:** release PostgreSQL is 17.11, while the retained owned lab pins 17.12. Restores across them stay within major version 17.

## Limits

- **No live writer activity:** no target was enabled, approved or written. KRR, Prometheus and the observer were not started in the release lab.
- **No browser trust or browser exercise.** Those are separate S7 gates.
- **Upgrade source:** B1 is the only earlier bundle-capable revision. Schema-crossing upgrade data therefore came from a source build of `f74d3ed`.
