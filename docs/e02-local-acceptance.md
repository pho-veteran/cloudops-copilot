# Integrated local workload acceptance

Use the existing [HTTPS/K3s lab](e01-local-lab.md), [scope configuration](workload-scope.md), [observer](workload-observation.md) and [healthy baseline](workload-baselines.md) instructions. Active acceptance and remaining gates live in [E02 #5](https://github.com/pho-veteran/cloudops-copilot/issues/5) and [integration task #29](https://github.com/pho-veteran/cloudops-copilot/issues/29). API receipts establish their recorded behavior; browser acceptance is recorded separately.

The integration probe uses the fixed `cloudops-e01-https` Compose project and separate `cloudops28_lab` application database. It never selects a host kubectl context. `demo/checkout`, its UID/spec and the original writer allowlist are protected by a captured hash. Only labeled `e02-ready`, `e02-recreate`, `e02-config` and `e02-unlisted` resources are created or faulted. The first three are read-scoped; `e02-unlisted` stays outside inventory. No new fixture receives writer authority.

## Prepare and run

Set the disposable lab's `POSTGRES_PASSWORD` using the existing setup. Build the current backend and HTTPS images before preparation. Verification requires a clean committed checkout and compares LF-normalized runtime and checkout source hashes against the exact Git blobs. `prepare` retains existing database records/volumes, creates the separate application database if absent and refreshes only the existing read-only service-account token. Only `tmp/e02-baselines/reader/{client,workloads}.json` is mounted into the API/worker/observer; the Compose database connection and denied-reader fixture stay outside that mount. It does not change RBAC or writer policy.

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml build api https
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml up -d db k3s
uv run --project services/backend python scripts/probe-e02-integration.py prepare
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f tmp/e02-baselines/compose.json --profile observation up -d --no-build api worker observer https
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f tmp/e02-baselines/compose.json exec api python -m cloudops.entrypoints.cli account e02-operator --role operator
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f tmp/e02-baselines/compose.json exec api python -m cloudops.entrypoints.cli account e02-viewer --role viewer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f tmp/e02-baselines/compose.json cp https:/data/caddy/pki/authorities/local/root.crt tmp/e02-baselines/root.crt
uv run --project services/backend python scripts/probe-e02-integration.py seed-legacy
uv run --project services/backend python scripts/probe-e02-integration.py verify --ca tmp/e02-baselines/root.crt
```

The account commands prompt for passwords and revoke prior sessions for these two test accounts. `verify` prompts for their existing passwords; `--password-stdin` accepts exactly two lines for automation. Passwords are consumed before child commands and are never receipt fields. The generated Compose file contains the database connection and stays ignored. If using a current-source overlay over a cached backend image, pass its explicit local image names to `prepare --backend-image ... --https-image ...` and record that distinction from clean CI builds.

`seed-legacy` creates or retains one explicitly synthetic, unverified v0 continuity fixture in this separate database; it never rewrites an existing confirmed record. Verification refuses an empty legacy check and reads the actual `legacy/v0` view. Repeat-at-head upgrade and stop/start preserve that nonempty fixture and the selected v1 record. Populated upgrades from old revisions are covered separately by the PostgreSQL `_test` suite; repeat-at-head is not claimed as that proof.

Verification first saves healthy v1 baselines for all three scoped disposable targets. It checks actor/profile/digest, both roles' exact recovery-context consumption, unchanged product domain rows and unchanged target UID/generation/resourceVersion/spec. It then changes generation, causes a real crash, swaps a ConfigMap reference and recreates a Deployment name. Negative cases require their exact error codes; busy or superseded reads cannot pass as candidate-change evidence. Wrong-pin rejection checks the specific CLI diagnostic. It verifies new UUID/UID isolation, retained history, denied reads, natural stale age, Viewer denial and restart persistence. Updates/deletes are conditional on current UID/resourceVersion, and existing ConfigMaps must have the expected owner. Guards remain active under optimized Python; the CLI additionally rejects `-O` so verification assertions cannot be skipped. A failure remains a failure: inspect the owned fixture before rerunning, rather than applying a manifest to `checkout`.

## Additional measurements and browser verification

Run the additional probes sequentially after `verify`; they share the fixed lab scope, observer and API settings. Each generates its own commit/source-bound receipt under ignored `tmp/e02-baselines`. The API probes prompt for the existing `e02-operator` password, or accept one line with `--password-stdin`. Do not change scope or restart the API while the natural-expiry race is waiting.

```sh
uv run --project services/backend python scripts/probe-e02-budget.py
uv run --project services/backend python scripts/probe-e02-isolation.py
uv run --project services/backend python scripts/probe-e02-partial.py
uv run --project services/backend python scripts/probe-e02-session.py
uv run --project services/backend python scripts/probe-e02-races.py
```

The 100-target measurement uses the original checkout, three running disposable targets and 96 zero-replica targets with bounded names/ownership. Its receipt records actual completion time, read windows, freshness and poll lag. Budget fixtures are removed afterward and the original read scope restored. This is a local measurement, not an availability SLO or a claim of 100 healthy applications.

The process-isolation measurement held three actual scheduled leases against a controlled real TLS drip-feed server in a separate probe process. A fourth scheduled lease was denied, a fresh direct K3s candidate used the reserved slot, and the actual HTTPS API/product worker completed an explicitly synthetic investigation while all three scheduled reads were still blocked. Each connection was closed by the normal 12-second deadline. This transport fault is not a live Kubernetes health source and supplies no execution proof.

The races traverse real Pod → ReplicaSet → Deployment ownership before restarting a process or conditionally deleting a Pod. They check same-Pod restart, Pod replacement and natural five-minute expiry with exact rejection reasons. The partial probe adds an unsupported downwardAPI projection, requires partial/unknown health and manual-only `unsupported` context, then restores healthy reads. The session probe temporarily configures actual 12-second sessions, verifies `401 → 409 → 201` across expiry/new-login/fresh-review, and restores and verifies the prior session configuration. API expiry is distinct from browser intent/focus behavior. Never run destructive pytest checks against `cloudops28_lab` or the original application database.

Browser completion requires the normal trusted HTTPS origin, with no certificate-warning bypass. Follow the established temporary CA approval/removal lifecycle. Exercise Workloads → exact identity/detail → owned health/related incidents, preview → confirm → exact selected history/context, Viewer read-only controls, failed/expired candidate recovery and session expiry/re-sign-in without replay. Confirm refresh/restart retain the selected workload and baseline IDs. Check actual 320/390 CSS widths, keyboard focus and readable error/limitation text; record screenshots and observation/network limits. API success does not pass this gate.

## Recorded API evidence

| Receipt | Recorded behavior | Scope limit |
|---|---|---|
| [Integrated API](evidence/e02-integrated-api.json) | Healthy preparation before faults, exact consumer, generation/reference/crash rejection, replacement, denial, natural staleness and restart | Actual backend and probe hashes are recorded; later browser evidence is separate |
| [Refresh budget](evidence/e02-integrated-budget.json) | 100-target measured refresh and read windows | Local measurement, not an availability SLO |
| [Process isolation](evidence/e02-integrated-isolation.json) | Three stalled TLS leases, reserved direct read and actual API/product-worker completion | Controlled observation transport; investigation evidence remains synthetic |
| [Candidate races](evidence/e02-integrated-races.json) | Same-Pod restart, Pod replacement and natural five-minute expiry | Owned disposable Pods only |
| [Partial context](evidence/e02-integrated-partial.json) | Final backend returns partial/unknown and manual-only unsupported, then recovers | Owned downwardAPI fixture only |
| [Session expiry](evidence/e02-integrated-session.json) | Real expired-cookie replay, new-login candidate rejection and fresh explicit confirmation | API proof; browser intent/focus behavior remains separate |

Source hashes are generated by the probes after normalizing CRLF to LF only, and compared with the exact committed Git blobs. The checkout must be clean at the start and end; no provenance fields are added manually to generated receipts. The API receipts truthfully retain their capture-time pending-browser status. The later browser and local-workflow records below cover the same backend/probe content plus the narrow-dialog correction. Acceptance and closure decisions remain in E02; historical receipts are not rewritten to claim later checks.

## Recorded browser and local workflow verification

The [actual Edge browser record](evidence/e02-browser/receipt.json) covers the normal trusted `https://localhost:8443` origin with no certificate-warning bypass. The live lab was recreated with the exact standard backend/HTTPS images built by the local workflow at `31afbf1`; the later repository-identity instruction changes documentation only. A healthy baseline was saved before fault injection. Investigation output remained explicitly synthetic and insufficient-evidence.

| Journey | Observed result | Screenshot |
|---|---|---|
| Explicit healthy preview and confirmation | Both observations pinned; saved ID/profile/digest/actor/time match the rendered record and actual recovery-owned GET. No selection initially returns manual-only/missing. | [390px preview](evidence/e02-browser/operator-preview-fixed-390.png), [320px context and keyboard focus](evidence/e02-browser/operator-selected-context-320.png) |
| Controlled crash and replacement without a baseline | Pending confirmation and new broken preview refuse with `baseline_unavailable`. Replacement has no inherited confirmed history and receives manual-only/missing. The retired UUID keeps readable history but cannot start an investigation. | [Refusal at 320px](evidence/e02-browser/broken-candidate-guidance-320.png), [missing-baseline guidance](evidence/e02-browser/replacement-broken-missing-390.jpg) |
| Natural stale age and recovery | Stopping the observer produces unknown/stale and Unknown readiness; retained evidence is labeled historical. Restart and explicit refresh restore fresh healthy observations and a new candidate. | [Stale state](evidence/e02-browser/stale-unknown-390.jpg) |
| Viewer inspection | Exact history/context/related incident remain readable; confirm/investigate/new-incident controls are absent. Confirmed rows, product domain rows and target versions/specifications remain unchanged. | [Read-only preview](evidence/e02-browser/viewer-candidate-readonly-390.jpg) |
| Actual browser session expiry | A temporary 45-second session receives 401 on authenticated reads, clears pending intent and focuses Username. Re-sign-in retains selection with no pending confirmation or new confirmed row. Only fresh explicit preview/confirmation returns 201. Prior 28800-second sessions are restored. | [Expired session](evidence/e02-browser/expired-session-focus-390.jpg), [fresh confirmed context](evidence/e02-browser/post-expiry-selected-context-390.jpg) |

Workloads → detail → investigate preselects the exact UUID. The saved incident and related-incident link round-trip to that same identity. Escape returns focus to the inventory trigger, or the Workloads heading for a direct link; Tab moves from explicit context refresh to Clear selection. Refresh/restart preserve exact workload and baseline IDs. A transient unavailable selected-context read recovered through explicit Refresh; it never offered execution eligibility.

The first 390px browser run exposed horizontal overflow from digest/history content. The scoped Workloads dialog correction uses bounded single-column grid tracks and wrapping; shared dialog primitives are unchanged. Rechecks at actual 320px and 390px show no horizontal overflow, including expanded JSON provenance. The record preserves the original failure and the corrected geometry.

The [local workflow receipt](evidence/e02-local-ci.json) records 612 PostgreSQL/unit tests, 85 web tests/build, Python lint/format, contract generation/deliberate drift, standard production image builds and the unchanged HTTPS smoke. It compares 206 verification-input files with the tested Git snapshot. GitHub Actions itself remains blocked before runner startup by account billing; the maintainer explicitly requested equivalent local checks as the merge gate. Linux Debian containers on the Windows Docker host replace the GitHub Ubuntu runner/actions wrappers. Host port 8444 isolates CI from the browser lab; the unchanged smoke uses localhost:8443 inside the CI HTTPS network namespace. An initial runner omitted stdin attachment; the complete corrected run passed without changing production smoke assertions.

## Cleanup

After recording browser evidence, remove only the named labeled fixture resources and reconcile their missing state:

```sh
uv run --project services/backend python scripts/probe-e02-integration.py cleanup
```

Cleanup checks the protected checkout/writer hashes before and after. It retains database history and all named database/K3s/executor-authority volumes. Stop the owned observer/API/worker/HTTPS and lab dependencies when unused, restore any temporarily refreshed original lab credential files, remove only the temporary CA trust and private probe credentials, and keep sanitized receipts. Executor scale regressions require separate trusted reconciliation and explicit plan/approval/restoration; finish with the original checkout spec and writer policy intact and the executor quarantined. Baseline preparation and recovery context never perform those authority transitions.

The [verified cleanup record](evidence/e02-cleanup.json) confirms owned fixtures removed, original checkout UID/spec/three replicas/writer policy preserved, original credential files restored, generated credential copies removed, and owned services stopped. Database, K3s and authority volumes retain 27 baselines and 8 incidents. The temporary CA was absent from both the current Windows user and LocalMachine Root stores at the final check; no additional certificate deletion was performed.
