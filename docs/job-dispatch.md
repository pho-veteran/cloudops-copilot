# Collection checkpoints and trusted job dispatch

Investigation collection commits each source as a durable checkpoint, and every background job class waits for a trusted dispatch permit stored outside PostgreSQL. This page is the operator handoff for [E04 #46](https://github.com/pho-veteran/cloudops-copilot/issues/46). It does not add a live source adapter, an analysis producer or a restore procedure; E04 C/D, E06 and E07/E10 own those.

## Collection checkpoints

A `development_collection_v1` attempt reads its configured sources one at a time in canonical sorted source-key order, matching the non-checkpointed batch publisher's cross-source allocation policy. After each read, one short PostgreSQL transaction commits the source's evidence row together with a `collect_source:<source_key>` checkpoint. The transaction requires the attempt's current lease token, an unexpired lease and the same permit generation that admitted the claim. A final `finalize_collection` stage inside the existing fenced finish transaction records `source_not_configured` outcomes for slots without an installed adapter, derives pack coverage, replaces latest-useful pins and completes the Run.

- **Takeover:** after a worker dies, the next claim reuses committed sources without calling their adapters again. Run ID, evidence IDs and hashes do not change. The Run shows how many sources an attempt saved and how many it reused.
- **Budget:** one job has at most three attempts. A transient source failure before the last attempt fails that attempt; the next attempt resumes at the failed source. Source retries consume the same budget; adapters perform no nested retries. On the last attempt a still-failing read is stored as an unavailable outcome (`timeout` or `source_unavailable`).
- **Fencing:** an expired, superseded or permit-retired attempt cannot checkpoint, finalize or overwrite committed evidence. The worker stops at the next boundary when its lease renewal fails, before another external call.
- **Cross-source bounds:** the Run-wide 10 Pod, 20 container and 20,000 sample budgets carry between checkpoints as digests. Batch publication and checkpointed collection both allocate in sorted source-key order, independent of catalog/adapter insertion order; takeover preserves that ordering and its retained payloads, hashes and coverage.
- **Failure:** if the budget is exhausted, the Run ends `failed` / `collection_failed`. Committed sources stay readable, pack coverage stays unrecorded and the latest-useful pin of an earlier Run is not replaced. Request a new investigation to collect again.

Migration `0009_job_checkpoints` adds `job_checkpoints`, permit evidence on `job_attempts`, and `investigation_runs.provider_call`. A job that was running during the upgrade has no checkpoints and collects from the start. Downgrade refuses to discard checkpoints.

## Cancellation boundaries

`POST /api/incidents/{incident_id}/runs/{run_id}/cancel` and `RunView.cancel` use one feature-declared rule:

| Run state | Result |
| --- | --- |
| `queued` (including a retry after earlier work) | Cancelled; no new attempt starts; already recorded work stays in history |
| `waiting_for_quota` with `provider_call = none` | Cancelled before analysis |
| `waiting_for_quota` with a call in flight or no declared boundary | `409 run_not_cancellable`: "A provider call is already in flight; it cannot be withdrawn." |
| `collecting` | `409`: collection already started; committed sources remain |
| `analyzing` | `409`: analysis already started |
| Finished | `409`: already finished |

No E06 analysis or quota producer exists. `waiting_for_quota` is exercised only with DB-seeded contract fixtures through the real API and guards; that is not live quota evidence. Resolving an incident still cancels only queued Runs.

## DispatchPermit/v1

`JOB_DISPATCH_ROOT` holds `permit.json`, `permit.lock` and an append-only `audit.jsonl`. Compose mounts the `job_dispatch` volume at `/run/cloudops-job-permit`: read-only for `api` and `worker`, writable only for the profile-gated `dispatch-admin` service, which has no database URL or writer credential.

```json
{
  "schema": "DispatchPermit/v1",
  "authority_id": "<uuid assigned at bootstrap>",
  "generation": 1,
  "mode": "normal",
  "allowed_job_kinds": ["collection", "recommendation", "recovery_execution"],
  "valid_until": "<UTC time>",
  "issued_at": "<UTC time>",
  "operation": "bootstrap"
}
```

`allowed_job_kinds` lists permit classes. The worker entrypoint maps the existing kinds: `development_collection_v1` → `collection`, `recommendation_report_v1` → `recommendation`, `recovery_execution_v1` → `recovery_execution`. Any other kind is never claimed.

Dispatch is denied, with a visible reason, when the permit is missing (`permit_missing`), unreadable or malformed (`permit_invalid`), expired or older than a generation the reader already saw (`permit_stale`), in recovery mode (`recovery_mode`), or does not list the job's class (`class_denied`). Database rows record which authority and generation admitted an attempt, but no database value grants permission.

Checks happen before deferred-Run promotion, inside each claim transaction, at each checkpoint and at final publication. Immediately before each source read, prepared KRR request or executor dispatch, a short call-start admission transaction holds the shared host permit lock and checks the live job token, unexpired lease and admitting permit generation. Successful admission is the call-start linearization point: recovery ordered first denies the call; admission ordered first may leave work in flight. Neither host nor PostgreSQL locks span the network call. Recovery does not promise physical network quiescence or abort already-admitted work; late results still cannot be published under a retired generation. The executor's own recovery mode, target epoch and final write check remain the authority for Kubernetes writes. Executor target quarantine does not block collection or reports in normal mode.

### Trusted administration

The CLI runs as an allowlisted OS account (`JOB_DISPATCH_ADMIN_UIDS`, default `0`) that owns `JOB_DISPATCH_ROOT`, which must not be group- or world-writable. It refuses to run on Windows. Every change takes the exclusive file lock, writes the new permit with fsync and atomic replace, and appends an audit line.

```sh
compose="docker compose"   # add -p/-f options for a lab project
$compose run --rm dispatch-admin status
# Fresh install only: refuses when any earlier permit or audit exists.
$compose run --rm dispatch-admin bootstrap --fresh-install --reason "Fresh install" --evidence "<ticket or runbook>"
$compose run --rm dispatch-admin enter-recovery --reason "..." --evidence "..."
$compose run --rm dispatch-admin resume --class collection --reason "..." --evidence "..."
$compose run --rm dispatch-admin exit-recovery --reason "..." --evidence "..."
$compose run --rm dispatch-admin renew --days 30 --reason "..." --evidence "..."
```

| Operation | Effect | Generation |
| --- | --- | --- |
| `bootstrap --fresh-install` | Normal mode, every class. Never reads PostgreSQL; never inferred from restored rows | 1 |
| `enter-recovery` | Deny every class; waits for in-progress short claim/checkpoint/call-start admission boundaries, not admitted network calls | +1 |
| `resume --class <class>` | Allow one more class while in recovery mode | +1 |
| `exit-recovery` | Normal mode, every class | +1 |
| `renew --days N` | Extend validity (default 30, at most 365); in-flight attempts stay publishable | unchanged |

`status` prints the permit, the current denial, the days remaining (`renewal_due` below 7 days) and the latest 25 audit entries. A collector-only installation needs only this bootstrap; it never needs executor or writer credentials. Existing labs without a permit are upgraded by the same bootstrap. `scripts/bootstrap-integration.py` issues it once for the K3s lab.

### Supported restore order

1. `dispatch-admin enter-recovery`, so no class can be promoted, claimed or published.
2. When the executor is configured, its trusted `maintenance enter_recovery` for every target. This also makes new approvals fail (`recovery_mode`).
3. Restore PostgreSQL by the supported procedure.
4. Resume classes explicitly with `dispatch-admin resume --class ...`. E07/E10 decide which classes resume and when. A write additionally requires the executor's `enable_target` and `exit_recovery_mode`; restored approvals reach the executor with a retired epoch and are refused.

This is a host trust boundary for the supported procedure. It does not detect an unannounced database rollback outside that procedure.

### Lock order and limits

- Host shared permit lock, then one short PostgreSQL transaction (promotion, claim, call-start admission, checkpoint, finish). The host lock is never requested while a database lock is held, and neither lock spans a network call.
- Inside PostgreSQL: claim/checkpoint/finish lock Job → Incident → Run → Evidence. Cancel/resolve lock Incident → Run and skip a locked Job, so they never wait for a claimer.
- Every container in the local image runs as root, so write isolation comes from the read-only mounts. Do not mount the volume writable into api, worker or test services.
- A permit attempt fenced by a generation change still counts toward its job's three attempts.
- POSIX hosts use shared/exclusive `flock`. Windows development hosts use a stricter exclusive byte-range lock for readers as well; deployments run on Linux.

## Exercised local behavior

Fresh worker processes in an owned PostgreSQL/API lab preserved three committed source IDs and digests across process loss, then completed all twelve catalog slots on attempt two with three reused checkpoints. Recovery ordered before start admission made zero adapter calls; a call admitted before recovery could return but could not publish under the retired generation. These checks use explicitly registered source/gateway contract fixtures, not live Kubernetes, KRR, AI or a production restore.

The [320px dark resumed view](evidence/job-dispatch/resumed-checkpoint-320-dark.png) shows actual persisted progress from that worker takeover in the running Edge. Viewer denial, expired-session no-replay behavior, keyboard cancellation and retained retry history were exercised separately; no browser permissions or certificate trust were changed.
