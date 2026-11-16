# Durable bounded diagnosis

New collection finalization saves coverage and admits one operational Analysis/Job in the same fenced transaction. It does not backfill historical Runs. Without an enabled, exactly qualified model and matching egress policy, collection remains `insufficient_evidence` / `analysis_unavailable`, with zero model requests. The default Compose configuration leaves diagnosis unconfigured.

The [Findings and Connections UI](diagnosis-findings.md) reads these stored APIs and integrates new comparison/handoff projections. The [finite acceptance campaign](diagnosis-acceptance.md) records actual provider compatibility and quality separately. Loopback fixtures establish lifecycle behavior, not model quality or live collection.

## Configuration and authority

API and worker read three trusted, secret-free files:

| Variable | Purpose |
| --- | --- |
| `DIAGNOSIS_PROFILE_FILE` | Existing `DiagnosisModelProfile/v1`; one endpoint/model, fixed transport limits and credential reference |
| `DIAGNOSIS_EGRESS_FILE` | Existing `EgressPolicy/v1`; destination/source allowlist, redaction and byte limits |
| `DIAGNOSIS_QUALIFICATION_FILE` | Optional `DiagnosisQualification/v1`; exact trusted cohort and evidence reference |

Missing configuration makes no HTTP calls. Invalid files fail closed. A credential named by the profile is supplied to the **worker only**. Never put it in API responses, frontend variables, images, qualification files or committed `.env` files.

The optional `compose.diagnosis.yaml` overlay mounts `DIAGNOSIS_CONFIG_DIR` read-only at `/run/cloudops-diagnosis` in API/worker. Name files `profile.json` and `egress.json`. Set `DIAGNOSIS_SECRET_ENV_FILE` to a private worker-only env file. If operational qualification exists, set `DIAGNOSIS_QUALIFICATION_PATH=/run/cloudops-diagnosis/qualification.json`; otherwise leave it empty. The overlay is used explicitly with `docker compose -f compose.yaml -f compose.diagnosis.yaml ...`. It does not choose a provider, pay for inference, deploy AWS resources or grant execution capability.

Operational mode requires a qualification record:

```json
{
  "schema_version": "DiagnosisQualification/v1",
  "cohort_id": "<actual-reviewed-cohort>",
  "evidence_reference": "<retained-cohort-evidence>",
  "identity": {"<exact keys from trusted deployment identity>": "<exact values>"}
}
```

The authenticated status endpoint exposes the secret-free identity to match. It includes config ID/version, endpoint label, behavioral digest, requested model, policy digest, prompt/context/validator versions and the fixed lifecycle budget version. This JSON example is a template, not a qualification. Only install a real qualification after the owning #72 rubric is demonstrated. The behavioral digest excludes activation mode: evaluation-to-operational promotion does not invalidate itself. Endpoint/model/wire/budget/policy changes invalidate the tuple. Every send and result settlement recheck current configuration; a successful schema probe never grants diagnostic quality.

## Worker and transaction boundaries

`diagnosis_analysis_v1` and `diagnosis_probe_v1` use the distinct `analysis` dispatch class. A process acquires the dedicated PostgreSQL advisory session lock (`742070`) before claiming a model Job. If busy, no model attempt is consumed; the worker can claim collection/recommendation/recovery jobs. Model capacity is separate from collection `queue_reason=capacity`.

The physical slot connection is verified and reused for fenced model transactions. The lock query is scoped to the current database. Losing the connection/guard cannot publish through another pool connection. Cleanup unlocks that session or invalidates it before returning it to the pool.

Lock order is host dispatch admission → Job → Run → Analysis/call → ordered Evidence pins. Cancellation/resolve already hold Incident/Run and skip locked Jobs rather than waiting in reverse order. No network call holds a PostgreSQL transaction or a host permit lock.

1. **Context commit:** pin the exact Knowledge selection when retrieval is available, build the scope/manifest and persist the complete private `PreparedContext` plus a separate safe summary. If the index is unavailable, do not fabricate a selection: build without runbook references and record `runbook_not_selected`. A gap in required context finishes without sending; otherwise an exactly qualified model may analyze the available evidence with that limitation. Acquire temporary Evidence pins. Restart reloads this exact context; it does not retrieve newer Knowledge, recollect or substitute evidence.
2. **Send admission:** check the live token, lease, dispatch authority/generation, physical guard, original deadline, configuration, phase egress and budgets; increment the durable counter and insert a reservation keyed to this lease token. Commit before HTTP.
3. **One call:** use the bounded compatible transport once. There is no nested retry loop.
4. **Atomic settlement:** validate the response and exact references first, then persist safe call metadata together with either the immutable result/terminal state or the next durable phase/defer. A third-attempt response has no receipt-only checkpoint that requires a fourth claim.

An admitted reservation without a committed settlement becomes `provider_outcome_unknown`; it is counted and never replayed automatically. A timeout/ambiguous transport error is also unknown. A remote request may outlive local timeout/lease loss. This system does not promise exactly-once inference, known cost for a lost response, or cancellation of remote compute.

The complete private context is a working copy for active attempts only. Completion, insufficiency, failure, cancellation, deadline expiry and trusted recovery clear it; immutable context manifests/digests, exact references and validated results remain. A terminal Analysis therefore does not retain a second telemetry payload after E04 purges the original. Temporary pins and original Evidence/Knowledge retention keep their existing ownership and policies.

## State, budgets and waits

| Outcome | Persisted behavior |
| --- | --- |
| Valid supported exact-reference advice | Immutable `RCAResult/v1`, completed Analysis/Run; advisory only |
| Valid model abstention | Immutable abstention result; Analysis/Run remain `insufficient_evidence` |
| Required context gap | `insufficient_evidence`, no HTTP |
| Repairable structural/JSON failure | At most one durable repair phase, bounded safe feedback; no raw rejected text resent |
| Invalid exact reference, refusal, unsupported shape, auth/config rejection | Typed terminal failure, no fallback provider |
| Configured worker credential echoed in validated advice | `credential_in_output`; no stored result or repair, including nested or JSON-escaped text |
| Received retryable 5xx | Bounded exponential due wait, no sleeping worker/held lease |
| 429 | `waiting_for_quota` / `resume_required`; explicit Operator resume after the earliest retry |
| Timeout, lost response, unsettled reservation | Terminal `provider_outcome_unknown`; new investigation required |

Each Analysis has an original 15-minute deadline, ≤3 admitted HTTP calls and ≤1 repair. Job attempts remain independently capped at 3; a context-stage crash can consume an attempt without a call. Each claim sends at most once. Repair itself is not repeatedly retried after a provider failure. Automatic delays use a 30-second base, exponential growth and bounded deterministic per-call jitter; a received Retry-After is a minimum, never shortened to fit the deadline. If another legal send cannot fit, the Analysis terminates.

Quota holds are not made eligible by the passage of time alone. Resume keeps context, counters, selection and deadline; it cannot change endpoint, UID, model, budget or output. Replaying the same actor/Analysis idempotency key does not create another Job/call. Early, expired, exhausted, changed-config, resolved or restored work returns a typed conflict.

Each normal worker cycle sweeps expired/retired model work **before** acquiring the slot. The sweep is due-filtered and bounded, independent of model send permission. An unresumed hold expires and releases temporary pins even when the slot stays busy. API GETs do not call providers or mutate expiry state.

Cancel is allowed while Analysis has no in-flight call, including a quota hold. Cancel and admission lock the same Run/Analysis boundary: if cancel wins, there is no send; if reservation wins, cancellation reports an in-flight conflict. Resolve retires unsent work. Already admitted valid advice may finish as historical data without reopening the Incident or creating a plan/execution. Existing target locks, manual plan approval and saved resolution bytes remain independent.

## Public API and consumer fixtures

All reads require a session and return `Cache-Control: no-store`. Mutations require Operator, the existing exact Origin/CSRF checks and a UUID `Idempotency-Key`. Bodies are empty or `{}`; extra scope/config/budget fields are rejected.

| Method/path | Behavior |
| --- | --- |
| `GET /api/incidents/{incident_id}/runs/{run_id}/analysis` | Exact operational AnalysisView, safe context/reference manifest, nullable validated result, attempts/calls/repair and wait history; legacy has `not_available` |
| `POST .../analysis/resume` | Resume an eligible persisted hold without resetting counters |
| `GET /api/diagnosis/status` | Safe identity, availability, dispatch, quality and last check; no network on read |
| `GET /api/diagnosis/checks` | Bounded history pagination: default25, maximum100, offset≤100000 |
| `POST /api/diagnosis/checks` | Queue one fixed harmless probe; one call, same slot/permit, 60-second producer cooldown and at most one active check |

Probe receipts separate transport success, fixed-marker schema success and quality. Missing usage is null/unavailable. Raw provider text, headers, request IDs and exception bodies are not copied to receipts. A probe creates no Run findings. `RunView.analysis` is a small availability/progress projection; its collection attempts/checkpoints retain their original semantics. The full AnalysisView owns model attempt/call history.

`contracts/fixtures/diagnosis/*.json` are serialized by the real AnalysisView producer during owned PostgreSQL/loopback behavioral tests. They are test inputs for #71, never product findings. Regenerate using:

```text
uv run --project services/backend python scripts/export-diagnosis-fixtures.py --owned-test-lab
uv run --project services/backend python scripts/contracts.py --write
```

Set `TEST_DATABASE_URL` to a dedicated owned database ending in `_test`; the exporter runs destructive fixture tests. Generate TypeScript/OpenAPI through the contract tool, not by hand.

## Trusted evaluation

With `mode=evaluation_only`, ordinary collection cannot admit operational findings. In an explicitly owned isolated `_test` lab, a trusted local driver may run:

```text
python -m cloudops.entrypoints.diagnosis_evaluate --owned-test-lab --run-id <collected-lab-run>
```

The driver uses the same Job/worker/context/egress/slot/budgets/validator. It preserves the original operational Run state. No HTTP field selects evaluation purpose. Operational result/Run projections exclude evaluation artifacts; public exact Knowledge selection resolution also rejects evaluation selections. Validation may internally resolve those exact references for a case artifact. This is a prequalification path, not a browser feature or an evaluation platform.

## Migration, permit upgrade and restore

`0012_diagnosis_lifecycle` follows `0011_knowledge_selections`. It adds diagnosis tables and nullable `jobs.due_at`; existing Job kinds/IDs/attempts, evidence, Knowledge and saved report bytes are not reset/backfilled. Populated diagnosis history blocks downgrade; use a supported backup restore instead of dropping retained results.

For an existing deployment whose external permit predates `analysis`, stop workers, apply the sequential migration, then use the existing Linux trusted authority with an audited reason/evidence:

```text
python -m cloudops.entrypoints.dispatch_admin enter-recovery --reason <reviewed-reason> --evidence <reference>
python -m cloudops.entrypoints.diagnosis_recovery
python -m cloudops.entrypoints.dispatch_admin resume --class analysis --reason <reviewed-reason> --evidence <reference>
python -m cloudops.entrypoints.dispatch_admin exit-recovery --reason <reviewed-reason> --evidence <reference>
```

The recovery command needs the writable, allowlisted Linux host authority and the database maintenance credential. It revokes sessions and drains restored model work while analysis remains denied. Keep API/workers stopped during maintenance. Do not delete/rebootstrap the permit or edit its files.

For restore: enter external recovery **before** restoring PostgreSQL, preserve that newer permit root outside the database backup, restore the backup, migrate to head, run `diagnosis_recovery`, then explicitly resume classes and exit recovery. Every admitted Analysis/check stores its host authority/generation. A changed epoch retires pre-recovery pending/held work and preserves unknown reservations; simply reopening the class cannot replay it. If cleanup fails or pending work remains, leave dispatch denied and inspect; do not start API/workers or bypass the step. Take a new backup before incompatible deployment changes.

The owned Linux restore probe runs `scripts/probe-diagnosis-lifecycle.py seed --output /receipts --owned-test-lab`, then an actual `pg_dump -Fc`, external `enter-recovery`, recreation/`pg_restore --exit-on-error` of the exact owned `_test` database, then the probe's `verify` stage. Mount the same external permit root and retained `/receipts` between stages; the seed rejects an existing populated lab. Its local fixture profile/private state stays in the lab; commit only the sanitized receipt. The probe verifies real saved handoff/data fingerprints, Job IDs/attempts, revoked sessions, retired pending/quota/unknown work and no replay after trusted resume. No real provider or Kubernetes writer is involved.

## Verification and cost limits

Run the focused lifecycle/process/permission tests, migration preservation/repeat checks, contract/drift checks, web tests/build and the repository HTTPS smoke. Actual wire/process/restore receipts supplement database tests; CI is checked on the final PR head.

The default network-free setup costs no model fees. The owned compatible HTTP server is a test fixture, not local inference. Real-provider calls require separately authorized configuration/egress and a real qualified cohort. Byte limits are not exact token or billing limits; nullable usage and unknown outcomes must not be presented as zero cost or known quota. Keep AWS services off when not needed; this feature provisions none.
