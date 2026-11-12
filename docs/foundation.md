# Foundation operation and contributor handoff

The local foundation stores application state and source-aware collection outcomes. By default, the only installed investigation adapter is an optional synthetic development fixture; uninstalled live source slots remain unavailable. Optional scoped Kubernetes and Prometheus providers are enabled only by explicit configuration. Scope and acceptance stay in [E00](https://github.com/pho-veteran/cloudops-copilot/issues/15) and the linked feature EPICs, rather than a second repository backlog.

Configured live workload health/context uses the separate [read-only workload observer](workload-observation.md). These rollout observations do not install investigation source adapters or authorize remediation.

## Delivered behavior and evidence

The integrated UI first landed in `47f09ff`; the verification/hardening wave in the same PR adds current contract checks, setup evidence and patched runtime dependencies. The PR and E00 record the final tested commit. These delivery notes include the completed F4 consumer verification; the maintainer directed that it be performed by the active implementer. No second contributor is required.

| Surface | Implemented behavior | Verification |
|---|---|---|
| Local setup | Compose web/API/worker/PostgreSQL; ordered migration and workload bootstrap; named persistent volume | Clean install and repeat migration/start checks |
| Access | Provision/reset CLI, scrypt passwords, server-side opaque cookie sessions, Origin/CSRF and backend Viewer denial | PostgreSQL API tests; Operator/Viewer browser sign-in |
| Admission | Atomic incident/run/job; actor-bound idempotency key; stored question and UTC window; bounded dispatch queue plus bounded deferred automatic Runs; one active run per incident; queued cancellation. See [investigation requests and bounded admission](incident-admission.md) | Concurrent duplicate admission, legacy replay across upgrade, queue-full, deferral/promotion, cancel/claim races and rollback tests |
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

### Stored-data incident handoff

The persisted incident workspace has a **Current handoff** and explicit **View saved resolution** selections in activity history. The preview is a readable, literal Markdown document; HTML and human-authored text are not executed. Copy and download use the exact loaded server Markdown, not a second client formatter. Successful notes and lifecycle transitions refresh the current preview automatically; explicit refresh includes other later changes. Saved selections never silently fall back to another resolution or Run and are not invalidated by those current-preview refreshes.

- `GET /api/incidents/{incident_id}/handoff/preview` returns the selected content, schema version and Markdown. The existing `/handoff` download returns the same Markdown bytes when the stored inputs are unchanged. Both roles may preview/copy/export; lifecycle and note writes remain Operator-only.
- Resolve freezes `HandoffSnapshot/v2` source-metadata content and Markdown with the reason, actor and timestamp in the same transaction as lifecycle invalidation and queued cancellation. Saved `HandoffSnapshot/v1` envelopes retain their original version, content and Markdown; current previews and new saves use v2. Reopen, later notes/Runs and renderer upgrades do not regenerate saved Markdown. Existing unversioned JSON-fence exports retain their original renderer and bytes. Unknown versions fail explicitly. Legacy evidence coverage is displayed as **Not captured (legacy)**, independently of whether its original content remains available or has expired.
- Identity, Runs, evidence references, action outcomes, checks, questions, notes and completeness/counts come from stored records. Creator/requester/plan-decision attribution includes usernames where recorded, with IDs retained. Missing impact, findings and citations are explicitly unavailable or unrecorded. Labeled findings fixtures validate stored Run/evidence references and derive citation pairs; they are composer contracts, not a live diagnosis producer.
- E07 owns execution eligibility. Terminal unknown needs matching terminal job/result proof; transient unknown, active, unsupported or ambiguous states fail closed. Eligible handoff still shows the unknown outcome, cross-incident plan attribution and retained target lock. Lifecycle changes do not grant writer authority or unlock a target.
- Activity is keyset-paginated, newest first, with actor/time and an explicit total. List items contain metadata and text, never embedded saved reports; read one selected report through the preview/export endpoints. Queries defer saved snapshots and evidence payloads so report composition does not load nested prior reports or unused payloads. A full report remains proportional to its disclosed stored records rather than silently truncating history, referenced records or held/uncertain locks. **Load older activity** keeps resolutions beyond the former 100-row boundary reachable.
- Preview loading/refresh and error-with-retry states retain explicit selection. A history refetch failure keeps earlier rows visible with an error and explicit retry. Copy failures remain visible; an older asynchronous copy completion cannot announce success for a different selected document. The report is keyboard-scrollable, uses 14px text and wraps without horizontal page overflow at 320/390px.

The [producer receipt](evidence/incident-handoff/producers.json) records an actual scoped K3s conditional scale and a terminal unknown caused by controlled executor unavailability. The [upgrade receipt](evidence/incident-handoff/upgrade.json) compares the pre-change legacy export and current preview/download. [History API proof](evidence/incident-handoff/history.json) and the [browser receipt](evidence/incident-handoff/browser.json) cover 118 activities, native clipboard/download byte equality, notes/resolve/reopen/later Run, explicit old-resolution selection, missing-selection recovery, API outage and Viewer denial. Browser coverage includes the running Microsoft Edge, with no Edge permission/trust changes.

- [Structured saved report in Edge at 320px](evidence/incident-handoff/edge-viewer-saved-320.jpg)
- [Legacy saved content at 390px in dark mode](evidence/incident-handoff/legacy-390-dark.jpg)
- [Loading in Edge at 320px](evidence/incident-handoff/edge-loading-320.png) and [actual API-outage retry](evidence/incident-handoff/api-outage-390.jpg)

The [correction checks](evidence/incident-handoff/corrections.json) bind the updated program sources to the complete backend/web suites, production HTTPS smoke, before/after regressions and a repeated Edge resolve/reopen/history-retry journey. Legacy and structured golden exports remain byte-identical. [Corrected actor and timestamp metadata at 320px](evidence/incident-handoff/corrected-metadata-320.png) uses the separate managed lab browser: Edge interactions worked, but relay screenshot capture timed out. No Edge permission or trust setting changed.

The browser uses a separate loopback HTTP development origin; API/producer probes validate the HTTPS chain and hostname with the lab CA. No browser certificate warning is bypassed. Investigation collection in the default stack remains synthetic; the optional scoped Prometheus metric provider is documented below. Scale acknowledgment is not verified remediation, and future-state/RCA fixtures are labeled separately. This D slice does not by itself complete E03 integration.

### Source-aware evidence and retention

The persisted workspace reads real stored Run/evidence records, not simulation state. `standard_v1` declares five Kubernetes slots, six fixed Prometheus query slots and the explicitly synthetic `development_fixture`. The default stack installs only the fixture adapter. The optional `live_kubernetes` provider uses the read-only server/ca_data/token JSON in `KUBE_READ_CONFIG`; ConfigMap metadata remains disabled unless explicitly enabled. The optional `prometheus` provider requires a trusted read configuration and is documented below. The live providers can be combined as `live_kubernetes,prometheus`; neither combines with the synthetic fixture. Compose and `.env.example` enable the fixture with `COLLECTION_SOURCE_MODE=development_fixture`; host processes without this setting default to `none`. `none` publishes honest `source_not_configured` outcomes for every slot. Unknown modes or adapter/query bindings fail closed, never silently selecting the fixture.

Each new completed batch stores all catalog outcomes and coverage for CrashLoop/startup, OOM/restarts, traffic latency/errors, Pending and ImagePull. Missing required sources and optional gaps stay distinct. Conditional logs can be `not_applicable`; a fixture never satisfies live pack coverage. Any completed collection, even all-unavailable, ends `insufficient_evidence` / `analysis_unavailable`. Only exhausted job/infrastructure failures end `failed` / `collection_failed`; historical null outcomes remain null.

The non-checkpointed runner uses the existing fenced `development_collection_v1` job and its locked attempt/max-attempts snapshot, not a separate retry budget. Reads run sequentially, below the four-read maximum, with a five-second adapter deadline and a 90-second collection deadline per attempt. Adapters must bound their actual underlying read; no timed-out background read is left running. Transient failures before the last attempt end that attempt within the existing at-most-three budget; the final attempt publishes unavailable source outcomes instead of inventing success. The worker now runs the same per-source policy through durable [collection checkpoints](job-dispatch.md#collection-checkpoints) behind a [trusted dispatch permit](job-dispatch.md#dispatchpermitv1). The optional live providers install the scoped Kubernetes and Prometheus slots; unconfigured providers remain explicit gaps.

New records are immutable per `(run_id, source_key, catalog_version)`. Identical publication preserves IDs; conflicting content/provenance is rejected. Storage independently whitelists, redacts credential-shaped strings, deterministically truncates and hashes the stored canonical JSON. ConfigMap evidence permits identifiers/references only, never full contents; arbitrary raw Kubernetes objects and Secret objects are not accepted. Redaction is not a general detector for every possible sensitive value. Historical v1 payloads/hashes are not rewritten by the new policy.

Canonical ordering and credential redaction do not reduce coverage by themselves. `truncated` means fields, items or text were actually omitted by an allowlist or storage bound, including unknown fields on fixture payloads; missing/invalid metric samples retain explicit `no_samples` semantics.

| Stored content allocation | Bound |
| --- | --- |
| Kubernetes state / events | 1 MiB / 512 KiB |
| Current / previous logs | 2 MiB each; 200 lines and 32 KiB UTF-8 per container |
| ConfigMap metadata / development fixture | 256 KiB each |
| Each of six metric query slots | 256 KiB; 25 series per query |
| Run-wide resources / samples | 10 Pods, 20 containers, 20,000 samples |
| All content / metadata envelopes | 7.5 MiB / 512 KiB; 8 MiB total |

Run list/detail evidence is **metadata-only**, including original coverage and a separate current `content_status`, pin and expiry projection. The viewer uses refreshed scoped Run metadata rather than a second stale cache of pin/availability, and fetches content only for the exact selected record. `GET /api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}` reads one metadata record; `/content` reads its bounded payload. Both roles may read. Wrong incident/Run/original UID relationships return 404, unavailable content returns 409 `evidence_unavailable`, and expired content returns 410 `evidence_expired`. Metadata and digest remain navigable after purge. Historical retired identities remain readable; this does not add per-user/tenant ownership.

Default payload retention is 30 days from collection. Resolution pins available referenced content in the same transaction as its frozen snapshot; already-expired references remain honest tombstones, not falsely retained payloads. Historical resolutions are pinned conservatively during migration. An unresolved incident additionally retains its last useful Run with stored available/partial content, even when analysis is unavailable. Failed/all-unavailable successors do not replace that pin. A later useful Run replaces only latest-useful pins, independently of saved-resolution or retained-reference pins; reopening selects the newest still-usable original-UID Run. Saved and unversioned legacy exports retain their original bytes.

Purge is deployment-owner maintenance, never an Operator/Viewer endpoint:

```sh
# Inspect candidates; no payload is changed.
docker compose exec api python -m cloudops.entrypoints.cli evidence-purge
# Explicitly clear only expired, unpinned payloads; metadata/digest remain.
docker compose exec api python -m cloudops.entrypoints.cli evidence-purge --apply
```

Pinning and purge lock evidence rows in the same order and recheck availability/pins under the lock. Repeating apply is idempotent. Destructive verification must use an owned database ending in `_test`, separate from application data and from a concurrently running pytest database.

Migration `0008_evidence_sources` preserves v1 IDs, payloads, hashes and frozen reports, maps them to `development_fixture` / `legacy_v1`, and does not invent historical windows or source coverage. It removes one-evidence-per-Run uniqueness and adds independent pins. Queued/running job IDs, kinds, tokens and attempt budgets are not migrated or reset. Downgrade refuses to discard evidence or captured coverage. Take a deployment-owner backup and stop old consumers before this breaking transport/schema cutover:

```sh
docker compose stop api worker web
docker compose build api web
docker compose run --rm migrate
docker compose up -d api worker web
```

Update external consumers to `source_key` and metadata-only evidence, fetching `/content` explicitly; do not mix old API/worker/web versions or hand-edit generated OpenAPI/TypeScript.

The local evidence exercise used real API/PostgreSQL/worker processes and the running Microsoft Edge 156 for admission, exact source selection, back/refresh, keyboard focus, 320/390px light/dark layouts, Viewer denial and session recovery. Normal restarted-worker Runs changed latest-useful pins without changing the selected old evidence. A 31,528-byte saved report remained byte-identical through later notes, reopening and new Runs; an isolated Viewer download matched those exact bytes. Trusted purge on a separate owned `_test` database reported one candidate, cleared one payload and then reported zero, preserving the expired record's digest and typed 410.

Partial redaction and historical legacy/expiry examples were explicitly controlled source-result/database fixtures, not live providers or a real 30-day wait. The images below show actual rendered Viewer records in a separate managed Chromium 150 capture context because relay screenshot capture was unavailable; Edge interactions remained the primary journey. No Edge permission or certificate trust was changed.

- [Available pinned content and provenance on desktop](evidence/evidence-storage/available-desktop.png)
- [Expired legacy metadata/digest at 390px in dark mode](evidence/evidence-storage/expired-390-dark.png)

Checkpoint progress, trusted dispatch pauses and safe cancellation have their own [API](evidence/job-dispatch/api.json) and [browser](evidence/job-dispatch/browser.json) receipts, with [paused queued work](evidence/job-dispatch/paused-queued-desktop.png), [collection after worker loss and API restart](evidence/job-dispatch/collecting-restart-320-dark.png) and [a failed Run offering reinvestigation](evidence/job-dispatch/failed-390.png). See [trusted job dispatch](job-dispatch.md).

### Scoped Kubernetes evidence

See [scoped Kubernetes investigation evidence](kubernetes-evidence.md) for reader setup, UID checks, bounded state/events/logs, optional ConfigMap metadata and the K3s/Edge acceptance receipt. AI diagnosis remains unavailable.

### Scoped Prometheus metric evidence

The optional `prometheus` provider installs the six fixed metric slots in the existing `standard_v1` catalog. It requires a trusted `PROMETHEUS_READ_CONFIG` with one fixed HTTPS endpoint, a verified CA, an optional bearer token and exactly the six configured query templates. Every returned series must bind the configured `deployment_uid` and `namespace` labels to the Run's immutable workload identity; unmatched series are dropped. The default `development_fixture`/`none` modes are unchanged and do not contact Prometheus.

Metric evidence uses the same bounded storage, retention, pin and checkpoint rules as other sources. Empty, gapped, non-finite, denied, timed-out, oversized and unproven-ownership results remain distinct from a real zero; at most 25 series per query and the existing 20,000-sample/8 MiB Run bounds apply. There is no new table or migration. The read-only Run timeline endpoint projects persisted E02 rollout observations, E03 activity and E07 execution timestamps without claiming causality.

The optional [metrics lab](prometheus-metrics.md#local-lab) adds a labeled metrics fixture behind the Caddy `/prometheus/*` TLS route and a staged config bootstrap. The fixture is not a product application exporter. [Integrated E04 evidence acceptance](e04-local-acceptance.md) records the five live Kubernetes/Prometheus pack journeys and the remaining proof-class limits. AI diagnosis, live E06 findings, recovery execution and release deployment remain with their owners.

### Work queue search and prioritization

The persisted work queue (`#/queue`) searches, filters, sorts and pages incidents through the read-only `GET /api/incidents` endpoint. Server-backed operational views keep incident lifecycle, alert signal, latest Run state and execution state as independent axes. The queue consumes E07's shared execution/lock classification and displays every relevant active, unknown or unsupported execution; it does not authorize or perform mutations.

The default queue shows open and acknowledged incidents, ordered by last-seen time descending with a stable incident-ID tie-break. The **All lifecycle states** choice includes resolved records. Operational views are **Needs attention**, **Awaiting decision**, **Verifying**, **Blocked** and **Resolved**. Blocked includes an actual capacity-deferred queued Run, waiting-for-quota, failed/insufficient-evidence Run history, and relevant uncertain/manual-intervention execution state; resolved incidents are excluded from that view. Active and unknown target-lock warnings remain visible on resolved incidents in other views. Severity and requester/acknowledgment attribution retain their stored provenance; missing historical values remain unknown.

Queue pages carry a server-generated creation-time/ID anchor, preserving its full timestamp precision, so inserts after the first page do not shift later offset pages. **Refresh new work** resets the anchor and returns to the first page. Mutable sort values such as last-seen can still move existing rows between pages; the queue does not promise a frozen snapshot. Filter, sort and view changes reset the offset, and the hash URL preserves the current query and selected incident when returning from detail.

- **Search** matches the title case-insensitively and literally (wildcard characters are escaped) or a prefix of the incident UUID. Terms are limited to 160 characters.
- **Filters:** state, alert signal, origin, severity (exact free-form value, up to 32 characters) and workload UUID. The workload field accepts a UUID; a value that is not UUID-shaped is never sent. A well-formed UUID that does not exist renders the API's error state instead of an empty result.
- **Sorting:** created time, first seen, last seen and severity, each with an `id ASC` tie-break so pagination does not skip or duplicate rows. Values without severity or seen times sort last. Severity is free-form, so severity sorting is alphabetical: choosing it requests ascending order (`A→Z`) and the select reflects the active direction. `sort_order` has no UI control but remains an optional URL parameter.
- **URL state:** search, filters, sort, offset, creation anchor and the selected incident persist in the hash (`#/queue?search=timeout&state=open`). Refresh, sharing, browser back/forward and the incident's **Work queue** link preserve them, and returning highlights the card that was opened. Only an internal queue route is accepted as a return target.
- **States:** distinct loading, error-with-retry, empty queue and no-match panels. An empty later page retains **Previous** because earlier pages may still contain work; **Refresh new work** starts a new anchored query. The queue refreshes every five seconds.
- **Workload history:** Related incidents explicitly includes all lifecycle states, including resolved incidents, in created-time order for the selected workload UUID.
- **Permissions:** the endpoint keeps read-only session authorization. The queue renders no mutation controls; incident creation stays owned by the application shell for operators.
- **Responsive layout:** search/filter/sort controls stack full width at 320px and 390px without horizontal scrolling, and cards keep titles, badges and identifiers readable.

Local evidence came from a disposable evidence stack with 34 seeded incidents: search at 320px, filters at 390px, severity sort, no-match, an error with retry after the API stopped, and URL preservation after returning from an incident.

- [Search at 320px](evidence/work-queue/search-320px.jpg)
- [State and alert filters at 390px](evidence/work-queue/filters-390px.jpg)
- [Severity sort ascending on page one](evidence/work-queue/severity-sort-desktop.jpg)
- [Unset severities last, on the final severity page](evidence/work-queue/severity-nulls-last-desktop.jpg)
- [No matching incidents](evidence/work-queue/no-match-state.jpg)
- [Error state with retry after the API stopped](evidence/work-queue/error-recovery.jpg)
- [Filters and selection preserved after returning from an incident](evidence/work-queue/url-preservation.jpg)

These images document local persisted-app behavior. They do not prove live cluster collection, AI diagnosis or execution.

Post-change acceptance uses a separate 70-incident labeled fixture set plus a real manual API arrival. The [browser receipt](evidence/work-queue/actual-browser.json) records 60 rows across three stable pages, microsecond anchor preservation, operational views, incident/Run/back navigation, resolved workload history, keyboard use at 320px/390px, session recovery and loading/refresh/error/empty/no-match states. The [API receipt](evidence/work-queue/actual-api.json) records server-side filters, acknowledgment attribution, metadata preservation, anchored new work and a Viewer write denial without an activity change.

- [Independent signal and lifecycle from the real sender at 320px](evidence/work-queue/actual-live-signal-only-320.jpg)
- [Retained unknown lock on a resolved incident at 390px](evidence/work-queue/actual-resolved-unknown-390.jpg)
- [Actual capacity deferral before promotion](evidence/work-queue/actual-capacity-320.jpg) and [the same Run after ordinary worker promotion and explicit refresh](evidence/work-queue/actual-same-run-promoted-320.jpg)
- [Keyboard-focused controls at 320px](evidence/work-queue/actual-keyboard-controls-320.jpg), [loading](evidence/work-queue/actual-loading-390.jpg), [refresh](evidence/work-queue/actual-refresh-390.jpg), [error with retained context](evidence/work-queue/actual-error-context-390.jpg) and [empty queue](evidence/work-queue/actual-empty-320.jpg)

The [real Alertmanager → intake → queue receipt](evidence/work-queue/actual-live-sender.json) uses the pinned sender, CA-validated HTTPS and a UID-bound K3s Deployment. Repeated firing retains one incident and its initial Run; resolution changes only the alert signal while the incident remains open. Its browser capture uses a separate loopback HTTP development origin, not a certificate bypass. Future execution/Run combinations in the other captures are explicitly labeled persisted fixtures, not live producers. Collection remains synthetic; this bounded B→C smoke does not complete E03-D or the full integration handoff.

## Configuration and account operations

Compose reads `.env`; see the committed `.env.example`. Use a URL-safe database password because the Compose connection URL interpolates it. Keep the exact loopback `WEB_ORIGIN` consistent with the browser URL. `COOKIE_SECURE=false` is an explicit HTTP-loopback exception; the default settings outside this file use Secure cookies. No CORS origin wildcard or public registration is enabled.

### Browser session configuration and HTTPS handoff

The API validates browser configuration before its database startup check and before serving requests. `WEB_ORIGIN` is one exact browser-serialized origin; requests must match it literally. The supported configuration pairs are:

| Origin example | `COOKIE_SECURE` | Use |
|---|---|---|
| `http://localhost:5173` | `false` | Local HTTP development |
| `http://127.0.0.1:5173` | `false` | Local HTTP development; also change the browser URL to this origin |
| `https://cloudops.example.com` | `true` | HTTPS deployment |
| `https://cloudops.example.com:8443` | `true` | HTTPS on a non-default port |
| `https://[2001:db8::1]:8443` | `true` | HTTPS with canonical bracketed IPv6 |

HTTP with Secure enabled is rejected by application policy, including on loopback. HTTPS with Secure disabled and non-loopback HTTP are rejected. `0.0.0.0`, LAN addresses, `*.localhost` and IPv6 loopback do not receive the HTTP exception. `COOKIE_SECURE` accepts only `true` or `false`, case-insensitively. There is no whitespace trimming or fallback for unrecognized values.

Use lowercase ASCII DNS names (IDNs in ASCII/Punycode form), canonical dotted-decimal IPv4 or compressed lowercase bracketed IPv6, and decimal ports from 1 through 65535. Omit explicit default ports (`:80` for HTTP, `:443` for HTTPS); browsers omit them in Origin. DNS labels must contain letters/digits/hyphens, have no leading/trailing hyphen, and fit DNS length bounds. A DNS trailing dot, when configured, remains part of the exact origin. Credentials, paths (including `/`), query/fragment (even empty `?`/`#`), percent escapes, backslashes, whitespace/control characters, alternate numeric IP spellings and multiple origins fail validation. Invalid input is not echoed in startup errors.

Session cookies are opaque, HttpOnly, host-only (no Domain), SameSite=Lax and Path=/. HTTPS sets Secure. `SESSION_SECONDS` is an absolute lifetime of 1..28800 seconds, default 28800; reading a session does not renew it. Logout deletes the server session and clears the cookie with matching scope/security attributes. Replaying a saved cookie after logout does not restore access. Session cookies belong only in HTTP cookie headers; session-bound CSRF tokens belong in the response and in-memory client state, then the `X-CSRF-Token` header. Do not put either authority in URLs or persistent browser storage, or include passwords/tokens/cookie headers in diagnostics.

Login requires exactly one configured Origin header. Authenticated mutations additionally require exactly one matching session-bound CSRF header. Missing/foreign/`null`/duplicate Origin yields `403 origin_denied`; missing/wrong/cross-session/duplicate CSRF yields `403 csrf_denied`. Missing or expired sessions on protected operations yield `401 session_expired`. These are checked before domain writes; rejected logout does not revoke a valid session. Host and forwarded headers do not select the trusted Origin or cookie flags. Reads do not require an Origin or CSRF header and remain session-authenticated.

For E10, configure for example:

```dotenv
WEB_ORIGIN=https://cloudops.example.com
COOKIE_SECURE=true
SESSION_SECONDS=28800
```

E10 must provide TLS and a same-origin frontend plus `/api` reverse proxy preserving API paths, cookie headers, the browser Origin and CSRF header. Keep the backend reachable only through the intended internal path; configure trusted proxy peers at deployment rather than treating arbitrary forwarded headers as authority. Check proxy/application logging rules so Cookie, Set-Cookie, CSRF and credentials are not logged.

E10's browser-through-proxy acceptance must demonstrate login, Secure host-only cookie round trip, session read, an authorized mutation, denial without the correct Origin/CSRF, and logout followed by unauthenticated session access. Inspect cookie scope/flags and verify the session token is not persisted in browser storage or URLs. The default Compose/Vite path remains HTTP development tooling. The local HTTPS exercise below supplies a real TLS/proxy request path; trusted-browser and release-topology checks still require their own evidence.

The configuration unit suite and `tests/test_access_browser_security.py` cover isolated parsing/startup and PostgreSQL-backed HTTPS TestClient behavior, including cookie set/delete attributes, token replay, session expiry, cross-session CSRF and denied-write counts. TestClient is API/fixture evidence; its cookie jar does not prove all browser SameSite/HttpOnly behavior, real TLS, proxy forwarding or deployment logging. Those remain E10 gates.

`config/development-workloads.json` contains at most 100 configured identities. Bootstrap validates the complete file before changing the active allowlist. Sync is transactional; removed rows become inactive rather than deleting incident history. A UID replacement cannot reuse the old persisted workload identity. The collector never reads cluster state or Secret contents.

```sh
# Create/reset an account, set its role, and revoke prior sessions.
docker compose exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
# Change only the role: keeps the stored password and account ID; previous sessions of that account are revoked.
docker compose exec api python -m cloudops.entrypoints.cli account-role viewer operator
# Revoke all active sessions of one account: keeps its password and role.
docker compose exec api python -m cloudops.entrypoints.cli revoke-sessions operator
# Apply current migrations and synchronize configured identities.
docker compose run --rm migrate
```

Unknown accounts fail with one `error:` line and change no data; an invalid role is rejected by argument parsing with a nonzero exit before anything runs. A committed role change or revocation makes later requests carrying a revoked session fail with 401. Requests already authenticated, and already admitted jobs, are not cancelled.

Sessions expire absolutely after `SESSION_SECONDS` (default 28800, maximum 8 hours), without sliding renewal. Logout revokes the server session. The browser holds CSRF authority in memory, never localStorage; only theme preference and the separate simulation use localStorage. A 401 removes protected query data, closes pending dialogs and requires sign-in at the same internal route. Mutations are not retried or replayed automatically.

### Browser navigation and account boundaries

Return navigation accepts only `#/queue`, `#/workloads`, `#/connections`, `#/reports`, and `#/incidents/<UUID>` with an optional `?run=<UUID>`. Unknown paths, external targets and additional parameters fall back to the queue. Signing in reads the selected saved records again; it does not retain a pending action.

Logout, expiry and account changes discard protected queries, mutation state and dialogs. The transport rejects responses from an earlier authentication generation, including a body that finishes parsing after a new account signs in. A late 401 from an old generation cannot end a new session. A current authenticated 403 shows the permission/CSRF error and does not sign the user out or retry the action.

Leaving a tab suspends its authority and clears protected views. Returning through focus, visibility or page restoration checks `/api/session` before rendering protected data or mutation controls. Failed connectivity shows an explicit retry state. The tab checks authority against the server; no cookie, CSRF token or principal is sent through localStorage or cross-tab messages. The sign-in username receives keyboard focus; Viewer access explains the unavailable incident/investigation actions.

The E07 consumer discards dialog input and pending approval intent at the same boundary. After sign-in it fetches the immutable plan/version, its live validity and a new session-bound review receipt, then requires a new explicit decision. The server rejects the pre-sign-in receipt even if its plan is still current. Changing the displayed plan/digest/validity closes the old approval intent. The [integration lab](e01-local-lab.md) verifies the actual approval, writer and maintenance paths; the default collector remains synthetic.

### Local HTTPS exercise

Use an isolated Compose project and a URL-safe `POSTGRES_PASSWORD` in `.env` as in README. The override fixes its origin to `https://localhost:8443` and enables Secure cookies for API, worker and migration. It removes the host API port, serves the compiled frontend through Caddy and leaves the Vite development service inactive. Only loopback port 8443 is published. PostgreSQL and CA material use project-specific named volumes; no cluster/model/executor is enabled.

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml up -d --build --wait --wait-timeout 120 api worker https
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml exec api python -m cloudops.entrypoints.cli account viewer --role viewer
mkdir -p tmp
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml cp https:/data/caddy/pki/authorities/local/root.crt tmp/local-https-root.crt
uv run --project services/backend python scripts/smoke-https.py --ca tmp/local-https-root.crt
```

PowerShell users can create `tmp` with `New-Item -ItemType Directory -Force tmp`. Use Docker Compose v2.24.4 or newer for the override. The smoke script prompts for the two account passwords, creates one labeled persisted incident and records only incident/run IDs in `tmp/https-smoke.json`. Automation may pass `--password-stdin` with Operator and Viewer passwords on separate stdin lines. No passwords belong in command arguments, output artifacts or source control.

The script validates the local certificate chain and hostname using the exported CA, then checks static frontend delivery, anonymous denial, configured Origin, CSRF denial without admission, Operator admission, Viewer reads/denials, cookie scope, logout and old-token replay. CI runs the same exercise with disposable accounts/database. The proxy's container health check establishes that its local CA exists; the smoke request establishes the actual TLS/API path.

For browser testing, open `https://localhost:8443` only after trusting this lab's root certificate through your normal development certificate setup. Trust is a separate local setup choice; the commands above do not alter OS/browser trust or disable certificate validation. Caddy access logging is disabled; its default runtime logs do not include request bodies or session headers. Do not enable request/header logging of credentials. This local TLS receipt does not establish browser SameSite/HttpOnly enforcement or a deployed release endpoint.

```sh
# Stop without losing local records or CA material.
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml down
```

No Kubernetes lab or E07 proof provider is created by this exercise. Maintenance recovery remains unavailable until the actual proof/current-state and trusted-channel integration is implemented and verified.

### E01 initial HTTP browser receipts

The 2026-10-26 exercise used the running PostgreSQL/API/worker with a separate loopback HTTP browser origin. Operator sign-in opened a saved incident and selected run. Signing out in another tab caused protected polling to return 401, closed the open creation dialog, displayed the expired-session message and retained the selected IDs. Keyboard sign-in as Viewer returned to fresh saved records with a read-only explanation and no mutation controls. Invalid credentials and intentional sign-out displayed distinct messages; refresh retained the selected run.

The 320px and 390px layouts were checked with actual browser device metrics and no horizontal document overflow. [Expired session](evidence/e01-expired-session.jpg), [invalid login at 320px](evidence/e01-login-320.jpg), [Viewer at 320px in dark mode](evidence/e01-viewer-320-dark.jpg) and [Viewer at 390px in dark mode](evidence/e01-viewer-390-dark.jpg) capture those states. Transport/query tests separately hold old responses and body parsing across account replacement, including late 401/403, and exercise focus/visibility authority checks and network retry. The browser's observed cross-tab expiry was driven by an actual rejected protected request; it is not claimed as isolated browser focus-event proof.

The HTTPS request exercise used certificate/hostname validation through the real local Caddy proxy. Browser screenshots use the separate HTTP development origin; no browser certificate warning was bypassed, OS trust changed, real cluster contacted or recovery proof claimed. Tested source commits, checks and remaining full-E01 gates are recorded in the PR and E01 tracker.

## Failure exercises

The initial HTTP screenshots above precede the [trusted HTTPS/K3s consumer exercise](e01-local-lab.md#reproduce-the-local-receipts), which now records the actual approval expiry/refetch, browser cookie/SameSite, Viewer export and maintenance integration receipts. Its temporary CA trust was explicitly approved and removed after verification.

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

For interruption, set `COLLECTOR_DELAY_SECONDS=60` on the worker with the same environment-override pattern, start a new run, then `docker compose kill -s SIGKILL worker` while it is collecting. Restore ordinary worker configuration. After the old 30-second lease expires, the next worker claims a new token. The old attempt becomes expired and cannot publish. Sources already checkpointed by the killed attempt are reused with their original evidence IDs. The process interruption tests reproduce this before and after a committed checkpoint with a dedicated test database and a real elapsed lease, without modifying its deadline.

To pause all background jobs, run `docker compose run --rm dispatch-admin enter-recovery --reason ... --evidence ...`. Queued Runs then show **Dispatch is paused** and stay cancellable; resume with `resume --class ...` or `exit-recovery`. See [trusted job dispatch](job-dispatch.md#trusted-administration).

**Connections** observes worker heartbeats with a 45-second freshness threshold. A stale worker is separate from database readiness. API readiness requires the current Alembic head; worker unavailability does not make saved reads unavailable. A database outage returns an actionable API error. A missing/failed schema prevents startup readiness; follow README migration recovery instructions instead of stamping a revision or deleting data.

## Interfaces and module ownership

| Owner | Implementation boundary | Consumers |
|---|---|---|
| E00 | `shared/config.py`, `shared/database.py`, Alembic and contract harness; `lib/api` transport | All implemented feature modules |
| E01 | `features/access`: principal/session/permission and account provisioning | Every protected API; application shell |
| E02 | `features/workloads`: configured stable identity, allowlist and known-good metadata | Admission, baseline preparation and Workloads UI |
| E03 | `features/incidents`: manual admission, notes/lifecycle and stored handoff | Work queue and investigation workspace |
| E04 | `features/investigations` and shared job mechanics | Worker composition and run/evidence UI |
| E05 | `features/knowledge` immutable corpus, index CLI, read catalog, [Run-pinned guidance](run-guidance.md) and [persisted Knowledge UI](knowledge-ui.md) | Optional semantic retrieval (unconfigured); E06 final context/egress/RCA |
| E06 | `features/diagnosis` bounded model transport and reusable fixtures; see [diagnosis-model.md](diagnosis-model.md) | Context/RCA validation, durable analysis, UI and live qualification |
| E07 | `features/recovery`: immutable manual plans, session-bound review, isolated executor and proof | Recovery UI, worker and trusted maintenance CLI |
| E08 | `features/recommendations`: bounded KRR jobs and stored reports | Report UI/export, worker and pinned read-only runner |
| E09 | `features/health` and `features/connections`: classified observations and fixed checks | Connections UI |
| E10 | `compose.integration.yaml`, scoped lab credentials and HTTPS/recovery handoff | Local runtime and integration probes |

HTTP handlers handle request/response/authentication; feature services own rules and transaction boundaries. Incident admission passes one SQLAlchemy session into E04, so partial incident/run/job admission cannot commit. Shared job code accepts feature handlers and never imports feature policy. Entrypoints register all persistence mappings before independent worker/CLI use. Network/synthetic collection work happens outside database transactions; fenced publication and job completion commit together.

Transport shapes live in feature Pydantic schemas, `contracts/openapi.json` and generated TypeScript. No hand-maintained DTO or second SDK is required. Only explicitly selected response fields reach the UI; password hashes, cookie tokens and job lease tokens are never response fields.

The implemented operation families are session/login/logout, workload inventory, manual incident create/list/detail, incident run create/list/detail and health observations. Use the generated artifact or the running [API documentation](http://localhost:8000/docs) for exact operations and field shapes.

- IDs are UUIDs; timestamps are UTC RFC3339. Run evidence contains versioned source/coverage/retention metadata; the scoped content endpoint supplies the stored payload and its canonical post-redaction SHA-256. Legacy content retains its original version/hash.
- List APIs use `limit` 1..100 (default 25), `offset` 0..100000 and `next_offset`. Incident/run order is creation time descending with UUID tie-break; workload order is UUID ascending. Concurrent inserts can shift offset pages; this foundation does not promise snapshot pagination.
- Operator mutations require exact Origin, session cookie, session-bound `X-CSRF-Token` and an `Idempotency-Key` UUID on admissions. The key is bound to actor, operation and canonical request. Identical replay returns the same IDs; a changed request with the same key returns 409. No client automatic mutation retry occurs.
- Errors use `{error: {code, message}}`: 401 for anonymous/expired session, 403 for Viewer/Origin/CSRF denial, 404 for missing or mismatched records, 409 for conflicts/unavailable content, 410 for expired evidence, 422 for invalid fields/scope and 503 for database unavailability.
- Collection uses `queued`, `collecting`, `insufficient_evidence`, `failed` and existing queued cancellation. Completed source collection ends `insufficient_evidence` / `analysis_unavailable`; exhausted infrastructure failures use `collection_failed`. Explicit reinvestigation creates a new Run. AI diagnosis remains unavailable; existing manual plan/execution APIs are separate and do not establish live investigation collection.

## Available-operation authorization checks

The test-only inventory in `services/backend/tests/permission_inventory.py` classifies
the operations actually served by the application. `permission_cases.py` provides
feature-specific valid requests and success assertions; `test_permissions.py` runs
every classified operation with anonymous, expired, Viewer and Operator sessions.
Authentication, cookies, role dependencies and PostgreSQL persistence remain real;
these tests do not override the principal or permission dependencies.

| Classification | Current operation IDs | Required behavior |
|---|---|---|
| Protected reads | `get_session`, `list_workloads`, `list_incidents`, `get_incident`, `list_runs`, `get_run`, `get_health`, `list_knowledge_documents`, `read_knowledge_document`, `read_knowledge_chunk`, `get_knowledge_index_status`, `get_run_guidance`, `resolve_run_guidance` | Viewer and Operator read existing workspace records; anonymous/expired requests return `401 session_expired` |
| Operator domain mutations | `create_incident`, `create_run`, `create_run_guidance`, `confirm_baseline`, `add_incident_note`, `transition_incident`, `create_connection_check`, `create_recommendation_report`, `propose_plan`, `decide_plan` | Valid Viewer requests return `403 permission_denied`; Operators may admit work subject to feature rules |
| Protected feature reads/exports | `list_baselines`, `list_incident_activity`, `preview_incident_handoff`, `export_incident_handoff`, `list_connection_checks`, `list_recommendation_reports`, `get_recommendation_report`, `export_recommendation_report`, `list_plans`, `list_executions` | Both roles inspect persisted records; no domain mutation |
| Protected observation | `get_baseline_candidate`, `get_recovery_context` | Both roles may save bounded read-only observation metadata; recovery context without a selection performs no live read. Neither grants execution authority or writes to Kubernetes |
| Protected review | `read_plan` | Both roles read current validity and receive a session-bound read receipt; a receipt grants no decision permission |
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
incident and original workload UID. Ordinary background workers must not target the pytest database, which tests clear.

Implemented consumer boundaries and their continuing feature owners:

| Owner | Operations requiring their own request-level evidence |
|---|---|
| E02 | Known-good baseline confirmation: Operator-only, with baseline validation and no change on denial |
| E03 | Notes and acknowledge/resolve/reopen: Operator-only, preserving lifecycle/state rules |
| E04 | Any new run controls: explicit semantics, run/job transition and denial evidence |
| E07 | Approval/rejection: Operator-only with current plan/state and fresh action; expired approval requires sign-in, plan refetch and no automatic replay |
| E08 | New report generation: Operator-only; Viewer may read/export existing permitted reports |
| E09 | New connection checks: Operator-only; Viewer may read existing permitted results |
| E01/E07/E10 | Isolated Linux executor maintenance: SO_PEERCRED deployment-root identity, durable request/proof provenance and actual UID/resourceVersion fence, never application role alone |

No absent operation is counted as verified. Permission tests use real auth/PostgreSQL with explicitly controlled source observations. The separate [local lab receipts](e01-local-lab.md) cover actual TLS, K3s, KRR, writer fencing and restore. Current acceptance and any remaining browser/deployment gates live in E01 and the owning feature EPICs; these checks do not establish model integration or production deployment security.

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

For isolated browser configuration checks without PostgreSQL, run `uv run --project services/backend pytest -q services/backend/tests_unit`. Both CI and the Compose test service include this suite as well as the PostgreSQL suite.

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

The optional E01 lab now supplies notes/lifecycle/handoff, live known-good metadata, bounded connection checks, persisted KRR jobs, exact-plan scale approval and proof-backed maintenance. The worker has only a private dispatch capability; the separate executor alone holds the scoped Kubernetes writer credential. Live investigation packs, comparison/runbooks, model analysis, additional recovery templates, qualified KRR history and release deployment remain in their feature EPICs. Authenticated Alertmanager intake with a lab-only pinned sender overlay is documented in [Alertmanager intake](alert-intake.md); integrated E03 acceptance remains with its EPIC. No E00 fixture result satisfies those gates, and no application role grants maintenance authority.

Fresh package audits are recorded with the tested PR commit. Existing web development-tool advisories remain an E10 release-tooling gate; a zero production-package audit is not a container/OS vulnerability scan or a complete security assessment. Only one maintainer has reviewed producer/consumer integration so far.
