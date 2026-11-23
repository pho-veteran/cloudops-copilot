# E10 S1 release package acceptance (#120)

This ledger records the S1 acceptance of the pinned Compose and HTTPS package for [#120](https://github.com/pho-veteran/cloudops-copilot/issues/120) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13). The package tooling comes from [PR #127](https://github.com/pho-veteran/cloudops-copilot/pull/127).

All runs took place on 2026-11-23 on a local Docker Desktop 29.9.2 Linux engine (amd64, cgroup v2, 8 CPU, 10.7 GiB) with a WSL2 Ubuntu 24.04 client. This is local evidence from owned projects. It does not cover an EC2 install, a cloud measurement or a browser exercise, which belong to [#121](https://github.com/pho-veteran/cloudops-copilot/issues/121) and [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).

## Identity

| Item | Value |
| --- | --- |
| Candidate revision (B2) | `3a94d1319e6dbd5d5b64d0c220a5fd85850c96af` |
| Previous release revision (B1) | `86649c0faec1bf8b5dde7fe1e16b371ef2967a1f` (main before this change) |
| B2 `source.tar` / `images.tar` SHA-256 | `edd583b2…a36c970` / `3b68aba8…42f222` (full values in `build.json`) |
| B2 image IDs (backend / https / db) | `sha256:86917b7c…cf0abf38` / `sha256:3cde299f…810a97e5` / `sha256:837bed18…fc03af3b` |
| Runtime bases (digests in the manifest) | `python:3.13.18-slim-bookworm`, `caddy:2.11.9-alpine`, `postgres:17.12-bookworm` |
| Build-only bases | `ghcr.io/astral-sh/uv:0.9.26`, `node:24.21.0-bookworm-slim` |
| Candidate CI | [Repository checks run 45137168780](https://github.com/pho-veteran/cloudops-copilot/actions/runs/45137168780) on the candidate passed web tests and build, backend, migrations, contract drift and the HTTPS smoke on PostgreSQL 17.12 |
| Backend suite | 2289 passed on Linux with the locked host environment. Inside the B2 backend image (Python 3.13.18, PostgreSQL 17.12), 2274 passed. The other 14 tests need `git` or `.env.example`, which the runtime image intentionally lacks. |

Commits after the candidate add only this evidence directory.

## Acceptance

| #120 requirement | Result | Receipt |
| --- | --- | --- |
| Clean isolated host/project starts pinned API, worker, web and DB | Each install removed release-tagged images first and loaded `images.tar` only. B1, then B2, went into new projects with no prior containers or volumes. Each started from an install directory holding only the extracted `source.tar`, with `--no-build --pull never`. `verify` and `verify --loaded` passed, and every container ran its manifest image ID. | `install.json` |
| Migrations apply with repeatability | A fresh database reached head `0020`. A same-core upgrade and a repeated start applied nothing further. The schema-crossing upgrade advanced `0017` → `0020`. | `install.json`, `upgrade.json` |
| Same-origin trusted HTTPS on compiled web | `scripts/smoke-https.py` passed against B1, B2 fresh, U1 and U2. Each run validated the certificate chain and hostname with the project CA. It checked the compiled `/assets/` page, Origin and CSRF denials, and Secure HttpOnly SameSite=Lax host-only cookies. It also checked Viewer denials, logout and token replay. The only published listener was `127.0.0.1:8443`. | `install.json`, `upgrade.json` |
| Loads scoped components from external credential references | A live collection in the release lab used the scoped reader. It stored Kubernetes state and events with content digests for an ImagePull fixture. Its logs are `not_applicable` because the container never started. The images contain no credential files. Credentials stay in the private `0700`/`0600` lab directory. | `separation.json` |
| Populated upgrade preserves records | U1 (B1 → B2, PostgreSQL 17.11 → 17.12 on the same volume): 42 of 45 tables are identical. U2 (source `f74d3ed` → B2): 41 of 45 tables are identical. The remaining tables differ only in scope synchronization counters (`observation_epoch`, `applied_sequence`, `latest_sequence`), the live worker heartbeat and, in U2, `alembic_version`. Column-level comparison against the pre-upgrade backup confirmed this. The HTTPS CA was unchanged. | `upgrade.json` |
| Generated contracts match backend models | `create_app().openapi()` inside the B2 backend image, run without a network, is byte-identical to `contracts/openapi.json` in the B2 source (`7d2b7076…`). TypeScript drift is checked in CI. | `upgrade.json` → `contract` |
| Writer credential and executor socket separated from API/worker | The lab ran only bundle images; no local build was used. API and worker have no writer credential or maintenance socket, and the API has no dispatch key. No container mounts the Docker socket. Real K3s returned 403 for reader patches, reader/writer Secret reads and writer patches outside the named target. The executor started in recovery mode with its target disabled. | `separation.json` |
| Sanitized configuration, logs and artifacts | Container logs of every release project and all raw and final receipts were scanned for the actual private values: passwords, the database password, reader/writer tokens, and the dispatch, KRR and intake keys. The scan also checked bearer, JWT, cookie and URL-credential patterns. No hits. | `resources.json` → `sanitization` |
| Reproducible build metadata, defined architecture/OS | Two B2 builds were compared: one on the default builder and one on an empty-cache `docker-container` builder. They had the same revision, upstream digests, `source.tar`, lock files, Python package set, backend application tree, compiled web tree and Caddyfile. Image IDs differ because of build metadata and are bound by each manifest. Bit-for-bit reproducibility is not claimed. | `build.json` |
| Port, storage and image requirements, deliberate stop | The bundle is about 285 MB (images 263 MB, source 22 MB). Loaded images: backend 385 MB, https 93 MB, db 646 MB. Idle core memory totals about 211 MiB, and startup takes about 16 s. `compose stop` kept all four volumes. A restart preserved data, CA and the dispatch permit. | `resources.json`, [requirements](../../release-package.md#requirements) |
| Default writer/dispatch safe, readiness separate from worker | Dispatch starts `paused/permit_missing` until the trusted `dispatch-admin bootstrap --fresh-install`. Upgrade pauses dispatch through `enter-recovery` until an explicit `exit-recovery`. With the worker stopped past its 45 s bound, readiness stayed 200 and health reported `worker: stale`. | `install.json`, `upgrade.json` |

## Dependency advisories

`advisories.json` holds the full details.

**Web npm (`apps/web`).** The baseline had 7 findings (1 high `body-parser`, 6 moderate), all in the `shadcn` CLI development chain. A lock-only `npm audit fix` cleared `body-parser`, `hono` and `@hono/node-server`. Four moderate findings remain in the `minimatch` → `ts-morph` → `shadcn` chain. The only fix is a breaking `shadcn` downgrade, so they are accepted as development-only. The production audit is 0, and the compiled `/srv` output contains none of these modules. The historical E00 set of seven high development-tool advisories no longer applies.

**Contract tooling npm (`tools/contracts`).** 0 findings.

**Backend Python.** `pip-audit` found `opentelemetry-api` 1.45.1 (CVE-2026-126360 / GHSA-mf5v-w3w3-f6cg). The lock now pins 1.45.2, and the candidate has 0 findings.

**Runtime images (Docker Scout, critical/high).** The bases were refreshed to the newest patch of each pinned line:

| Image | Before | After |
| --- | --- | --- |
| backend | 8C / 52H | 0C / 20H |
| https | 29C / 109H | 1C / 8H |
| db | 4C / 50H | 4C / 50H |

These remaining findings are accepted and recorded for the E10 release gate:

- **backend:** 14 Debian findings have no fixed version. The 6 PyPI items are copies vendored in the base image's system pip, which the application environment cannot import.
- **https:** Caddy 2.11.9 is the newest upstream image, and the remaining Alpine/Go fixes are not yet published in a Caddy image.
- **db:** Debian findings without a fix, plus the Go stdlib of the `gosu` entrypoint helper. No newer PostgreSQL 17 image changes these.

Exposure is limited to the loopback HTTPS origin and the private Compose network. Refresh the bases when upstream fixes arrive, then repeat this proof.

## Handoff to #121 and #124

**Install inputs:**

1. Build on a trusted Linux/amd64 host with `scripts/release-bundle.py build`.
2. Transfer the bundle and verify it before extraction, then run `verify --loaded`.
3. Start the core with the documented `compose` function (`compose.yaml`, `compose.https.yaml`, `compose.release.yaml`) and a private `host.env`.
4. Provision Operator and Viewer through the CLI, then copy the project CA for checks.

**Owned resources per core project:**

- Volumes `database`, `job_dispatch`, `local_https_data` and `local_https_config`.
- Published listener `127.0.0.1:8443`.
- Three images named `cloudops-release-{backend,https,db}:<revision>-<id>`.

**Upgrade and restore order:**

1. Verify and load the new bundle.
2. Take a `pg_dump` backup.
3. Run `enter-recovery`.
4. Stop the worker, then API and HTTPS.
5. Start the new bundle on the same project.
6. Check readiness, the smoke and saved records.
7. Run `exit-recovery`.

**Scoped lab topology:** see [scoped lab with release images](../../release-package.md#scoped-lab-with-release-images). The executor and every target stay disabled until the E07 maintenance proof. Release PostgreSQL now matches the retained owned lab's 17.12 minor version.

## Limits

- No target was enabled, approved or written. KRR, Prometheus and the observer were not started in the release lab.
- No browser trust or browser exercise was performed. Those are separate S7 gates.
- B1 is the only earlier revision that can build a bundle, so the schema-crossing upgrade data came from a source build of `f74d3ed`. That build used its own image names and replaced no shared local tag.
