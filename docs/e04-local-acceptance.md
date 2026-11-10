# Integrated E04 evidence acceptance

Acceptance record for [E04 #50](https://github.com/pho-veteran/cloudops-copilot/issues/50) and parent [E04 #7](https://github.com/pho-veteran/cloudops-copilot/issues/7). This record covers the five fixed investigation packs, the actual configured `live_kubernetes` and `prometheus` producers, normal admission/worker/checkpoints, stored evidence/citation/comparison/timeline consumers, restart and role/session behavior. It is not an E06 diagnosis, recovery engine, E07/E10 restore qualification or cloud release claim.

## Source and environment binding

- Implementation base: `f6941c9` (`#47` Kubernetes collectors merged; `#48` Prometheus metrics/timeline merged at `bd455ed`).
- Acceptance run: owned Docker Compose project `cloudops-e04-50`, separate database, K3s `v1.37.1+k3s1`, PostgreSQL `17.11-bookworm`, Prometheus `v3.5.0`, Caddy `2.10.2-alpine`, Python `3.13.11`, Node `24.21.0`.
- Producer mode: `COLLECTION_SOURCE_MODE=live_kubernetes,prometheus` with deployment-owned `KUBE_READ_CONFIG` and `PROMETHEUS_READ_CONFIG`; no fixture fallback.
- Browser: Microsoft Edge launched through agent-browser on the loopback HTTP development origin. No certificate/profile/trust bypass.
- The final commit and source/image hashes are in [`source-binding.json`](evidence/e04-integration/source-binding.json); the API/browser receipts were captured from the working-tree candidate before the final evidence commit.

## Verification summary

- Backend: 1154 collected, **1144 passed / 10 skipped** in two commands (916+10 skipped without `test_permissions.py`, 228 permissions tests); focused E04 suite 8 tests passed 10/10 consecutive runs.
- Web: 194 tests / 22 files passed; production web build passed.
- Ruff check/format, generated contract check and deliberate drift rejection passed.
- [`checks.json`](evidence/e04-integration/checks.json) records the exact commands and counts; [`cleanup.json`](evidence/e04-integration/cleanup.json) records removed owned resources.

## Reproduce the live five-pack run

```sh
export POSTGRES_PASSWORD='<disposable-local-password>'
export CLOUDOPS_LAB_PROJECT=cloudops-e04-50
LAB="docker compose -p $CLOUDOPS_LAB_PROJECT -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f compose.metrics.yaml -f compose.e04.yaml"

uv run --project services/backend python scripts/bootstrap-integration.py
$LAB up -d --build --wait --wait-timeout 300 --no-deps https prometheus metrics-fixture
uv run --project services/backend python scripts/bootstrap-metrics.py
$LAB up -d --build --wait --wait-timeout 300 api worker
```

`bootstrap-integration.py` applies `config/lab/workload.yaml` and `config/lab/kubernetes-incident-families.yaml`, waits for one Pod per fixture, records all five Deployment UIDs in the trusted reader scope and writes the Prometheus workload list. `bootstrap-metrics.py` copies only the Caddy CA into the ignored lab folder and writes the scoped read config.

Provision two disposable accounts and run the probe:

```sh
$LAB exec -T api python -m cloudops.entrypoints.cli account operator --role operator --password-stdin
$LAB exec -T api python -m cloudops.entrypoints.cli account viewer --role viewer --password-stdin
printf '%s\n%s\n' "$OPERATOR_PASSWORD" "$VIEWER_PASSWORD" |
  uv run --project services/backend python scripts/probe-e04-integration.py \
    --ca tmp/e01-integration/prometheus/ca.crt --password-stdin --restart
```

The probe creates one explicit one-minute Run per pack plus a second traffic Run for comparison. It reads every available content payload and verifies the canonical post-redaction SHA-256, checks pack coverage, compares 12 sources, resolves an exact sample citation, reads the timeline as Operator and Viewer, checks wrong-scope 404, restarts API/worker and confirms the same evidence IDs/hashes.

## Five-pack result

[`acceptance.json`](evidence/e04-integration/acceptance.json) records the actual Run/evidence IDs and all 12 source outcomes per pack.

| Pack | Live Run result | Required gap | Optional gap |
|---|---|---|---|
| `crashloop_startup` | `partial` | `kubernetes.logs.previous` unavailable | `kubernetes.configmap.metadata` (`source_not_configured`) |
| `oom_restarts` | `partial` | `kubernetes.logs.previous` unavailable | `kubernetes.configmap.metadata` (`source_not_configured`) |
| `traffic_latency_errors` | `available` | none | `kubernetes.configmap.metadata` |
| `pending_unschedulable` | `available` | none; current/previous logs `not_applicable` | `kubernetes.configmap.metadata` |
| `image_pull` | `available` | none; current/previous logs `not_applicable` | `kubernetes.configmap.metadata` |

The two partial CrashLoop/OOM packs are honest live gaps: K3s containerd returned “unable to retrieve container logs” for the previous container; the collector published `partial`/`no_samples` rather than a zero or an empty success. State/events/current logs and all six metric sources were available. Traffic, Pending and ImagePull packs were fully available. `kubernetes.configmap.metadata` remained `source_not_configured` because no ConfigMap metadata grant was enabled for this run.

## Checkpoints, restart, dispatch and evidence consumers

- The integrated backend suite (`tests/test_e04_integration.py`) covers batch = checkpointed = takeover parity for all 12 sources, including a crash at a Prometheus source, six reused evidence IDs/hashes, progress/finalization, five-pack matrices, exact citations for state/event/log/series/sample, timeline markers, failed-reinvestigation pin preservation, Viewer/Anonymous behavior and read-only side effects.
- The live probe created and finalized all five pack Runs through normal admission/worker/checkpoints; `--restart` preserved every stored evidence ID and SHA-256.
- [`dispatch.json`](evidence/e04-integration/dispatch.json) records the actual collection-class worker takeover and trusted `dispatch-admin enter-recovery` / `resume --class recommendation` / `resume --class collection` / `exit-recovery` behavior in a separate fresh owned project. `tests/test_dispatch_gate.py` covers all three current job classes, DB-seeded future-state rejection and stale-generation fencing. The executor restore/unknown path remains a separate E07/E10 gate; no unknown lock was released or written.
- A repeated Run with a failed/all-unavailable successor leaves the previous latest-useful evidence and pin readable; the live UI/API evidence remains historical, not a new RCA.

## Browser and role/session checks

[`browser.json`](evidence/e04-integration/browser.json) records Edge checks at 1280/390/320 CSS pixels, light and dark:

- Five pack evidence sources inspected; crashloop/OOM/Pending/ImagePull state screenshots and the traffic metric chart/table.
- Traffic chart at 320/390/1280; chart is 640 CSS pixels inside a focusable horizontal scroll frame, labels readable, no document-level horizontal overflow.
- Compare view loaded same-identity Runs; source rows disclosed `changed`/`unchanged`; numeric sample differences correctly remained `not_comparable` for unequal captured windows. An exact left sample citation target opened.
- Timeline markers rendered and remained visible with stored metric evidence.
- Viewer could read chart/table/timeline and had no note or lifecycle mutation form.
- Revoking all server sessions preserved the exact Run/evidence route and required sign-in; no mutation was replayed.
- No Edge profile permission, certificate trust or warning bypass was changed.

Screenshots are under `docs/evidence/e04-integration/` (`traffic-*`, `crashloop-state-*`, `oom-state-*`, `pending-state-*`, `imagepull-state-*`, `compare-*`, `citation-*`, `viewer-*`, `session-expired-*`).

## E04 parent AC ledger

| Parent AC | Proof |
|---|---|
| AC1 · Five scoped packs with required/optional coverage and no invented zeros | Live five-pack Runs, pack rows above, bounded state/events/logs and six metric sources; CrashLoop/OOM previous-log gaps explicit |
| AC2 · Exact citation navigation and deterministic immutable comparison | Live compare of two 12-source Runs; exact metric sample citation; `test_e04_integration` structured/log/series citations; no original hash/pin changes |
| AC3 · Restart/checkpoint/takeover durability and stale-worker fencing | Batch/checkpoint/takeover parity, live probe restart hash match, real worker kill/takeover in `dispatch.json` and `test_collection_checkpoints` |
| AC4 · Honest cancellation and no false provider/write cancellation | Live queued/collecting cancellation boundary in `dispatch.json`; `waiting_for_quota` remains a DB-seeded contract fixture, not an E06 producer |
| AC5 · Bounded/redacted logs, no scope expansion/Secret reads/injector truth | Live K3s state/events/logs under reader RBAC; previous-log gap explicit; ConfigMap metadata disabled; no Secret or injector path |
| AC6 · Failed reinvestigation leaves prior useful evidence readable | `test_e04_integration` useful Run → failed successor → unchanged latest-useful pin and readable original content |
| AC7 · Recovery/restore dispatch requires independent class permits | Actual collection-class takeover/class resumes in `dispatch.json`; all three classes and DB future-state rejection in `test_dispatch_gate`; E07/E10 restore/unknown gates remain separate |

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
- CrashLoop/OOM previous logs were unavailable from the local K3s containerd log path; partial pack coverage is the truthful result. No zero or empty success was fabricated.
- No AI diagnosis, E06 producer, E07/E10 restore/unknown release, cloud install or release-complete claim is made. Child/issue acceptance does not itself close E04.
