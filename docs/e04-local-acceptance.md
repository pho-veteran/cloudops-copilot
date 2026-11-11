# Integrated E04 evidence acceptance

Acceptance record for [E04 #50](https://github.com/pho-veteran/cloudops-copilot/issues/50) and parent [E04 #7](https://github.com/pho-veteran/cloudops-copilot/issues/7). This record covers the five fixed investigation packs, the actual configured `live_kubernetes` and `prometheus` producers, normal admission/worker/checkpoints, stored evidence/citation/comparison/timeline consumers, restart and role/session behavior. It is not an E06 diagnosis, recovery engine, E07/E10 restore qualification or cloud release claim.

## Final acceptance verification

The current [acceptance binding](evidence/e04-integration/final/manifest.json) supersedes the earlier desktop citation captures. The older `verified-browser.json` desktop entries show loading, so they are retained as historical attempts and do not establish resolved desktop target visibility.

- [Fresh live collection](evidence/e04-integration/final/acceptance.json): all five packs through normal API/worker/checkpoints, immutable evidence IDs/digests after restart, a guarded same-UID revision 1 → 2, deterministic repeat/Swap, exact citations and saved export preservation. CrashLoop/OOM remain partial for missing previous logs; the other three packs are available. No missing sample is replaced with zero.
- The fixture revision write now reads and verifies the API-selected Deployment UID and uses atomic JSON Patch UID/resourceVersion tests. [Actual K3s rejection](evidence/e04-integration/final/atomic-guard.json) and two regressions cover wrong UID/stale version boundaries without changing the template.
- [Fresh restore](evidence/e04-integration/final/restore.json) repeats real PostgreSQL rollback, independent three-class permits and retired-epoch refusal with the unknown lock retained. [Provider outage](evidence/e04-integration/final/outage.json) preserves 13 exact Run/content/timeline/comparison/saved-export reads after Prometheus stops.
- [Fresh user-connected Edge proof](evidence/e04-integration/final/browser.json): resolved revision/value/digest, keyboard focus and complete target geometry at 1280/390/320 in light/dark; real comparison selection, Back, Swap, Viewer and protected-read 401 route preservation. Final screenshots were captured after the navigation and paint settled, then visually inspected. Examples: [desktop dark](evidence/e04-integration/final/citation-1280-dark.jpg), [320 light](evidence/e04-integration/final/citation-320-light.jpg).
- The Edge download-event helper timed out; no fresh Edge file-byte capture is claimed. Actual prior IAB downloads remain separately recorded in `verified-browser.json`, whose source/receipt binding matched the original candidate. Fresh API saved-export SHA-256 `7f24ee3fcb0300f1d7b3260777a4f1ab9a3ddb38df1a4fabb91135a30dc6de44` remains unchanged through revision/comparison/restart/provider outage. This distinguishes stored-byte preservation, prior download proof and current Edge capture limits.
- [Checks and diagnostics](evidence/e04-integration/final/checks.json): one clean local run of **1,158 backend tests**, **195 web tests**, build, Ruff (193 files), contracts and deliberate drift rejection. The first fresh-image bootstrap failure was reproduced; suppressing build progress keeps admin status valid JSON. [The absent-image path](evidence/e04-integration/final/bootstrap.json) issues all three fresh-install permits without replacing existing authority.
- [Runtime binding](evidence/e04-integration/final/runtime-binding.json) matches actual API/web source hashes and launch-driver hashes to the fixed source. Earlier receipts keep their original source binding and are not relabeled as this run.

These results establish the scoped E04 acceptance task. They do not certify production telemetry, E06 diagnosis, E07/E10 executor/cloud restore or release completion.

## Original source and environment binding

- Implementation base: `f6941c9` (`#47` Kubernetes collectors merged; `#48` Prometheus metrics/timeline merged at `bd455ed`).
- Acceptance run: owned Docker Compose project `cloudops-e04-50`, separate database, K3s `v1.37.1+k3s1`, PostgreSQL `17.11-bookworm`, Prometheus `v3.5.0`, Caddy `2.10.2-alpine`, Python `3.13.11`, Node `24.21.0`.
- Producer mode: `COLLECTION_SOURCE_MODE=live_kubernetes,prometheus` with deployment-owned `KUBE_READ_CONFIG` and `PROMETHEUS_READ_CONFIG`; no fixture fallback.
- Browser: Microsoft Edge launched through agent-browser on the loopback HTTP development origin. No certificate/profile/trust bypass.
- The original candidate and source/image hashes are in [`source-binding.json`](evidence/e04-integration/source-binding.json). Those receipts remain historical; follow-up corrections and their separately bound evidence are below.

## Original verification summary

- Backend: 1155 collected, **1145 passed / 10 skipped** in two commands (917+10 skipped without `test_permissions.py`, 228 permissions tests); focused E04 suite 9 tests passed 10/10 consecutive runs.
- Web: 194 tests / 22 files passed; production web build passed.
- Ruff check/format, generated contract check and deliberate drift rejection passed.
- [`checks.json`](evidence/e04-integration/checks.json) records the exact commands and counts; [`cleanup.json`](evidence/e04-integration/cleanup.json) records removed owned resources.

## Reproduce the live five-pack run

```sh
export POSTGRES_PASSWORD='<disposable-local-password>'
export CLOUDOPS_LAB_PROJECT=cloudops-e04-fix57
LAB="docker compose -p $CLOUDOPS_LAB_PROJECT -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f compose.metrics.yaml -f compose.e04.yaml --profile http-development"

uv run --project services/backend python scripts/bootstrap-integration.py
$LAB up -d --build --wait --wait-timeout 300 --no-deps https prometheus metrics-fixture
uv run --project services/backend python scripts/bootstrap-metrics.py
$LAB up -d --build --wait --wait-timeout 300 api worker web
```

`bootstrap-integration.py` applies `config/lab/workload.yaml` and `config/lab/kubernetes-incident-families.yaml`, waits for one Pod per fixture, records all five Deployment UIDs in the trusted reader scope and writes the Prometheus workload list. `bootstrap-metrics.py` copies only the Caddy CA into the ignored lab folder and writes the scoped read config.

`compose.e04.yaml` configures API/worker for the HTTP development origin before web starts. The HTTPS proxy remains the CA-validated, fixed Prometheus read destination; browser/API requests use the Vite HTTP origin. Set two disposable local account passwords, then provision them and run the probe from the repository root:

```sh
printf '%s\n' "$OPERATOR_PASSWORD" | $LAB exec -T api python -m cloudops.entrypoints.cli account operator --role operator --password-stdin
printf '%s\n' "$VIEWER_PASSWORD" | $LAB exec -T api python -m cloudops.entrypoints.cli account viewer --role viewer --password-stdin
printf '%s\n%s\n' "$OPERATOR_PASSWORD" "$VIEWER_PASSWORD" |
  uv run --project services/backend python scripts/probe-e04-integration.py \
    --origin http://localhost:5173 \
    --ca tmp/e01-integration/prometheus/ca.crt --password-stdin --restart
```

The probe waits until actual Prometheus range results cover a full minute. It creates one explicit one-minute Run per pack, saves/resolves the traffic incident, freezes its saved export, reopens it and changes the owned checkout Pod template through the lab K3s container. A second Run under the same Deployment UID captures the new revision. Per-source state differences must repeat identically and reverse under Swap; exact revision citations resolve both sides. The saved export and all first-Run evidence IDs/hashes must survive comparison and API/worker restart. Numeric sample alignment for unequal windows remains a separate `not_comparable` case.

For the provider-outage check, stop the owned `prometheus` service after a Run is stored and re-read its Run, available evidence content, timeline and 12-source comparison from PostgreSQL/API; [`provider-outage.json`](evidence/e04-integration/provider-outage.json) records the exact incident/Run IDs and the unchanged hashes. No provider is contacted by those reads.

Vite serves `http://localhost:5173` and proxies the API. No temporary Origin override, certificate warning or trust bypass is needed. Use a fresh owned project; lab bootstrap writes scoped credentials into the ignored `tmp/e01-integration` directory, so do not run it concurrently with another lab using that folder.

### Reproduce the shared-dispatch restore drill

```sh
# Requires the backend image built above and a new project name.
uv run --project services/backend python scripts/probe-e04-restore.py \
  --project cloudops-e04-restore-57 --output tmp/e04-restore.json
```

This driver refuses existing project containers/volumes and only restores its dedicated `cloudops_e04_restore_test` database. It seeds current collection/report/approved-execution records, takes an actual `pg_dump -Fc`, dispatches all three through the real worker handlers, enters recovery, then uses `pg_restore --clean` to bring the queued records back. No class may dispatch until explicitly resumed. Each class then dispatches alone; the restored execution reaches a controlled loopback HTTP gateway that rejects its retired epoch and retains `outcome_unknown`/the lock. KRR and executor transports are labeled fixtures without a write capability; the drill proves shared worker/class-permit behavior across actual PostgreSQL rollback, not actual KRR execution or E07/E10 executor recovery qualification. The permit volume is independent of the restored database.

## Original five-pack result

[`acceptance.json`](evidence/e04-integration/acceptance.json) records the actual Run/evidence IDs and all 12 source outcomes per pack.

| Pack | Live Run result | Required gap | Optional gap |
|---|---|---|---|
| `crashloop_startup` | `partial` | `kubernetes.logs.previous` partial/`no_samples` (content stored but incomplete) | `kubernetes.configmap.metadata` (`source_not_configured`) |
| `oom_restarts` | `partial` | `kubernetes.logs.previous` partial/`no_samples` (content stored but incomplete) | `kubernetes.configmap.metadata` (`source_not_configured`) |
| `traffic_latency_errors` | `available` | none | `kubernetes.configmap.metadata` |
| `pending_unschedulable` | `available` | none; current/previous logs `not_applicable` | `kubernetes.configmap.metadata` |
| `image_pull` | `available` | none; current/previous logs `not_applicable` | `kubernetes.configmap.metadata` |

The two partial CrashLoop/OOM packs are honest live gaps: the local K3s previous-container log read returned an incomplete/non-timestamped segment; the collector retained available content but published `partial`/`no_samples`, not a zero or an empty success. State/events/current logs and all six metric sources were available. Traffic, Pending and ImagePull packs were fully available. `kubernetes.configmap.metadata` remained `source_not_configured` because no ConfigMap metadata grant was enabled for this run. The synthetic `development_fixture` slot is intentionally `source_not_configured` in `live_kubernetes,prometheus` mode; it is a labeled synthetic source, not a live producer and not part of the five-pack availability claim.

## Checkpoints, restart, dispatch and evidence consumers

- The integrated backend suite (`tests/test_e04_integration.py`) covers batch = checkpointed = takeover parity for all 12 sources, including a crash at a Prometheus source, six reused evidence IDs/hashes, progress/finalization, five-pack matrices, exact citations for state/event/log/series/sample, timeline markers, failed-reinvestigation pin preservation, Viewer/Anonymous behavior and read-only side effects.
- A terminal failed successor after provider failure leaves the prior Run's content, timeline and comparison readable, with its stored SHA-256 values unchanged; the same test asserts stored handoff preview/export bytes are equal.
- A separate live outage receipt ([`provider-outage.json`](evidence/e04-integration/provider-outage.json)) stopped the Prometheus service in a fresh owned lab: the existing traffic Run, its available content hashes, timeline and 12-source comparison remained identical before/after.
- The injector canary test puts injector annotations, environment values and a Secret reference in the controlled Kubernetes input; none of the canary values enter any stored evidence payload or digest. ConfigMap metadata remains metadata-only.
- The live probe created and finalized all five pack Runs through normal admission/worker/checkpoints; `--restart` preserved every stored evidence ID and SHA-256.
- [`dispatch.json`](evidence/e04-integration/dispatch.json) records the actual collection-class worker takeover and trusted `dispatch-admin enter-recovery` / `resume --class recommendation` / `resume --class collection` / `exit-recovery` behavior in a separate fresh owned project. That dispatch project runs `COLLECTION_SOURCE_MODE=development_fixture`; it proves job lifecycle/class-permit behavior, not live producer collection. The five-pack project separately proves the live Kubernetes/Prometheus producers. The restart-entry order is the supported procedure in [trusted job dispatch](job-dispatch.md#supported-restore-order): fresh-install bootstrap, `enter-recovery` before restore, explicit class resumption, then `exit-recovery`; the executor maintenance half was not configured in this base lab and remains an E07/E10 gate. `tests/test_dispatch_gate.py` covers all three current job classes, DB-seeded future-state rejection and stale-generation fencing. The executor restore/unknown path remains a separate E07/E10 gate; no unknown lock was released or written.
- A repeated Run with a failed/all-unavailable successor leaves the previous latest-useful evidence and pin readable; the live UI/API evidence remains historical, not a new RCA.

## Browser and role/session checks

[`browser.json`](evidence/e04-integration/browser.json) records Edge checks at 1280/390/320 CSS pixels, light and dark:

- Five pack evidence sources inspected; crashloop/OOM/Pending/ImagePull state screenshots and the traffic metric chart/table.
- Traffic chart at 320/390/1280 in both themes; chart is 640 CSS pixels inside a focusable horizontal scroll frame, labels readable, no document-level horizontal overflow. Dark was toggled through the application control and verified with `html.dark` and body background `rgb(16, 18, 22)`; light is `rgb(247, 248, 250)`. Light/dark image hashes differ.
- Compare view loaded same-identity Runs; source rows disclosed `changed`/`unchanged`; numeric sample differences correctly remained `not_comparable` for unequal captured windows. An exact left sample citation target opened.
- Timeline markers rendered and remained visible with stored metric evidence.
- Viewer could read chart/table/timeline and had no note or lifecycle mutation form.
- Revoking all server sessions preserved the exact Run/evidence route and required sign-in; no mutation was replayed.
- No Edge profile permission, certificate trust or warning bypass was changed.

Screenshots are under `docs/evidence/e04-integration/` (`traffic-*`, `crashloop-state-*`, `oom-state-*`, `pending-state-*`, `imagepull-state-*`, `compare-*`, `citation-*`, `viewer-*`, `session-expired-*`).

## E04 parent AC ledger

| Parent AC | Proof |
|---|---|
| AC1 · Five scoped packs with required/optional coverage and no invented zeros | Live five-pack Runs, pack rows above, bounded state/events/logs and six metric sources; CrashLoop/OOM previous-log gaps explicit; every stored row reports `standard_v1` from the configured catalog, not adapter merge order |
| AC2 · Exact citation navigation and deterministic immutable comparison | Follow-up live template/revision change under one UID, per-source semantic detail, repeat/Swap and exact revision citations in `verified-acceptance.json`; mobile/desktop focus and actual comparison/saved-export downloads in `verified-browser.json`; frozen saved-export regression through failed reinvestigation |
| AC3 · Restart/checkpoint/takeover durability and stale-worker fencing | Batch/checkpoint/takeover parity, live probe restart hash match, real worker kill/takeover in `dispatch.json` and `test_collection_checkpoints`; all process/concurrency tests use the owned `cloudops_test` database |
| AC4 · Honest cancellation and no false provider/write cancellation | Live queued/collecting cancellation boundary in `dispatch.json`; `waiting_for_quota` remains a DB-seeded contract fixture, not an E06 producer |
| AC5 · Bounded/redacted logs, no scope expansion/Secret reads/injector truth | Live K3s state/events/logs under reader RBAC; previous-log gap explicit; ConfigMap metadata disabled; injector canary and Secret reference absent from every stored payload; `test_evidence_redaction` covers source allocations, truncation and the 20,000-sample/8 MiB caps; no Secret read path |
| AC6 · Failed reinvestigation leaves prior useful evidence readable | `test_e04_integration` useful Run → failed successor → unchanged latest-useful pin, readable content/timeline/compare and stored export |
| AC7 · Recovery/restore dispatch requires independent class permits | Actual `pg_dump`/`pg_restore` rollback and real three-class worker dispatch in `verified-restore.json`; no calls before class permits, class isolation and restored-epoch denial retain the unknown lock. Current records/providers are labeled fixtures; broader executor/cloud restore qualification remains E07/E10 |

## Follow-up acceptance after the completeness review

The follow-up adds one integrated backend regression, one web regression, an actual shared-dispatch restore driver and a citation navigation correction. No database migration or transport schema changes are introduced.

- [`verified-acceptance.json`](evidence/e04-integration/verified-acceptance.json): fresh owned `cloudops-e04-fix57` K3s/Prometheus collection of all five packs, a template environment change under the same Deployment UID, state detail comparison with repeated/swap results and exact revision citations, saved export preservation and API/worker restart equality. The driver hash and images at launch are in the receipt.
- [`verified-ui-input.json`](evidence/e04-integration/verified-ui-input.json): the first follow-up collection, retained because the browser intentionally reads that earlier historical pair (revision 1 → 2). The later API repeat uses a different pair; IDs are not silently substituted between receipts.
- [`verified-restore.json`](evidence/e04-integration/verified-restore.json): actual custom-format PostgreSQL snapshot/restore in an owned `_test` database, independent permit volume, zero calls after rollback until resumption, one matching class call per resume and a retired-epoch gateway refusal with `outcome_unknown`/lock retained. The real worker handlers use controlled loopback KRR/gateway fixtures, not a live executor or a Kubernetes write.
- [`verified-browser.json`](evidence/e04-integration/verified-browser.json): actual Codex in-app browser at 320/390/1280 light/dark, explicitly authorized fallback after the running Edge client twice stopped because it could not verify the URL. Edge reached sign-in/comparison but its incomplete walkthrough is not promoted into the completed browser proof. No trust/profile permission bypass occurred.
- The browser uses real comparison/citation links, keyboard Return, browser Back and Swap; pair/filter context survives. Each screenshot captures the actual revision row or focused exact target inside the viewport, rather than the Run header. Viewer reads work without a mutation form; session revocation requires sign-in while preserving the exact URL.
- The actual saved-handoff download is 10,746 bytes with SHA-256 `456664fe2c4bb509ed0ed225c867701a3cc73576b9a3ee3221681b42e1c8a7f9`, identical to its pre-reinvestigation API baseline. Comparison JSON is also downloaded through the UI. Filenames/IDs and bytes are task-local fixtures, not production data.

### Citation navigation correction

An exact target used to sit below the full lazy payload. Loading a large payload and the document's smooth scrolling could leave the focused citation far outside the viewport. Citation targets now precede full content; the parent evidence heading does not steal exact-target focus. Exact navigation focuses without automatic smooth scrolling and explicitly exposes the target immediately. The new web regression delays a large payload; real browser checks verify both focus and target geometry at each required viewport/theme.

### Follow-up checks and unsuccessful attempts

- Linux backend: 1,156 collected. The first container run passed 1,139 but lacked repo scripts/Compose/docs for 17 existing wiring/probe tests (14 setup errors, 3 failures). After mounting those files, all 17 passed. Thus every collected case passed across the initial run and focused environment-corrected repeat; this is not a claim of a single clean full invocation. No case was waived or skipped.
- Web: 195 tests in 22 files passed, TypeScript check and production build passed after the focus correction. Existing JSDOM scroll warnings are not browser evidence.
- Ruff check/format, generated contracts and deliberate drift checks passed. [`verified-checks.json`](evidence/e04-integration/verified-checks.json) records checks; [`verified-source-binding.json`](evidence/e04-integration/verified-source-binding.json) records the follow-up source/receipt/image binding.
- The first restore-driver attempt asserted the attempt state `succeeded` against the Job row, whose successful state is `completed`; the driver assertion was corrected and the full drill repeated in a fresh project. Failed owned projects were kept separate from the passing receipt.
- The new image-change test respects anonymous template-object identities (removed/added image fields) while requiring a changed stable Deployment revision. Existing locator keys and stored evidence bytes are not rewritten to force a match.
- Intermediate browser captures were rejected when the focus/viewport predicate had not settled. The committed `verified-*` captures were regenerated after the navigation correction and passed the geometry checks.

The original outage, process-loss/takeover and future-state fixture receipts remain separately scoped. Completing this task does not itself declare the entire E04/E07/E10 release complete or release unknown execution locks.

## Failed attempts and corrections

- An initial probe used a 15-minute window immediately after Prometheus started; metric sources were `partial/no_samples` because the captured window began before the fixture history. The probe was corrected to a one-minute explicit window after the lab was warm; this was a harness window error, not a producer pass.
- The first live probe asserted every target pack must be `available` and stopped on CrashLoop/OOM. The real K3s previous-log retrieval gap is explicit; the probe was corrected to reject only `unavailable` and to require `available` for the traffic metric pack. CrashLoop/OOM remain `partial` with missing required previous logs.
- `scripts/probe-job-dispatch.py` printed a relative path through `Path.relative_to(ROOT)` and raised after writing the receipt; the reporting line was corrected. The receipt itself was valid.
- The Edge comparison form required re-filling/re-clicking after React state updates; the browser route was then driven explicitly with the validated `compare_incident`/`compare_run` URL. No product behavior was modified for this harness correction.
- The earlier #48 browser receipts are historical and not reused as #50 evidence; this record uses fresh Edge captures.

## Cleanup and retained state

- Remove only the owned lab projects and fixtures; do not touch shared containers, host Kubernetes contexts or unrelated volumes.
- Accounts provisioned: `operator` and `viewer` in each owned project; no credentials are stored in receipts.
- Retained for audit during this acceptance: the owned PostgreSQL/K3s/dispatch volumes; they are not application production data.
- Final cleanup receipt: [`cleanup.json`](evidence/e04-integration/cleanup.json); source/image binding: [`source-binding.json`](evidence/e04-integration/source-binding.json).

## Limits

- The K3s incident families are controlled local fixtures, not production telemetry. The live proof establishes scoped collection and storage behavior, not a production SLO.
- CrashLoop/OOM previous logs were partial/`no_samples` from the local K3s containerd log path; stored content was retained but marked incomplete. No zero or empty success was fabricated.
- No AI diagnosis, E06 producer, E07/E10 restore/unknown release, cloud install or release-complete claim is made. Child/issue acceptance does not itself close E04.
