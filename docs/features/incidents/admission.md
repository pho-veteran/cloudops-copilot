# Investigation requests and bounded admission

This page describes how incidents and investigation Runs are admitted. Scope and acceptance live in [#35](https://github.com/pho-veteran/cloudops-copilot/issues/35) and [E03 #6](https://github.com/pho-veteran/cloudops-copilot/issues/6). Collection is still the labeled synthetic fixture. A stored request window does not prove that telemetry covers that window.

## Request contract

Every new Run stores an immutable request:

| Field | Values |
|---|---|
| Question | Operator text; the incident title (`title_default`) for a three-field create; `automatic` for trusted intake; `not_captured` for Runs created before this change |
| Window | Absolute UTC start/end plus its source: `preset_15`, `preset_30`, `preset_60`, `explicit`, `legacy_default`, `automatic_default`. Earlier Runs keep no window rather than an invented one |
| Origin | `operator` with the signed-in actor, or `alertmanager` with a server-configured source ID. HTTP callers cannot choose either |
| Queue reason | `capacity` while an accepted automatic Run waits for a dispatch slot |
| Outcome code | `operator_cancelled`, `incident_resolved`, `scope_retired` or `execution_blocking` when pending work is cancelled |

Window rules:

- Presets are 15, 30 or 60 minutes (default 30). The server resolves them once, at the first admission, using database time.
- An explicit interval needs timezone-aware timestamps, spans 1 to 60 minutes, and cannot end in the future. Invalid windows return `422 invalid_window` and admit nothing.

`POST /api/incidents` still accepts the earlier body with `workload_id`, `title` and an optional `description`. Such a request gets the title as its question and a labeled 30-minute default window. `POST /api/incidents/{id}/runs` now requires `{workload_id, reason, window}`. The `workload_id` must be the incident's own workload (`409 target_mismatch`). A resolved incident must be reopened first (`409 incident_resolved`). An empty body only replays an admission that was already committed; a new empty body returns `422 run_reason_required`.

## Retries and idempotency

The idempotency key is scoped to the actor. Repeating the same key with the same input returns the original incident/Run IDs and window, and the Run's current state, even if capacity or scope changed meanwhile. The same key with different input returns `409 idempotency_conflict`.

Requests committed before this change keep their original signature. The legacy three-field create and the empty-body Run replay byte-for-byte. New requests use a versioned canonical signature; an explicit window is hashed in UTC, so the same instant written with another offset is the same request. The web app keeps a pending key across dialog close/remount after an uncertain network failure. It discards the key after a definite answer or a session change.

## Capacity

| Bound | Meaning | Configuration |
|---|---|---|
| J | Queued or running Jobs of every kind (collection, recovery, reports) | `queue_limit`, default 100 |
| D | Accepted automatic Runs waiting with `queue_reason=capacity` | `DEFERRED_RUN_LIMIT`, integer 1–1000, default 100; API and worker refuse to start with another value |

- Manual admission with J full returns `409 queue_full`. Manual work is never deferred.
- Automatic admission (`admit_automatic`, used by [Alertmanager intake](alert-intake.md)) with J full and D available saves the incident and one `queued/capacity` Run without a Job. With both full it returns `503 capacity_exhausted` and saves nothing. The caller's `lookup` runs before any capacity check, so an accepted delivery always replays its original IDs.
- Each worker cycle promotes up to five deferred Runs, oldest first, before claiming work. Promotion rechecks the incident, workload scope and recovery executions, invalidates draft plans, and creates exactly one Job. Promotion survives restarts because the deferred Run is persisted. There is no fairness guarantee against a continuous stream of other admissions.

## Cancellation, resolution and claims

- **Cancel:** an Operator can cancel a `queued` Run, deferred or waiting for a worker. The Run becomes `cancelled`, its Job is retired without an attempt, and a `run_cancelled` activity records the actor and optional reason. Cancelling again returns the same Run. A `collecting` Run returns `409 run_not_cancellable`; its result is kept as history.
- **Resolve:** resolution cancels queued Runs (`incident_resolved`) in the same transaction as the saved snapshot. A Run already collecting may finish; its evidence is stored, and it never reopens the incident or schedules more work.
- **Claim:** before each new attempt, including a reclaim after lease expiry, the worker rechecks eligibility. A cancelled Run, a resolved incident, a retired workload identity or a blocking execution retires the Job without an attempt. Collector callbacks never change a cancelled Run.
- **Plans:** a draft plan proposed while a Run waits is invalidated at promotion, and one proposed after the Job exists is invalidated at claim. Approval returns `409 run_active` while the incident has a queued or collecting Run, before the review receipt is consumed.

Execution states are classified in `features/recovery/classification.py`. `queued`, `executing` and the declared future active states block new Runs, promotion, claims and resolution. An unrecognized state also blocks. A terminal unknown outcome allows a read-only investigation and keeps its target lock.

## Lock order

Admission and promotion serialize on one advisory lock. Cancellation and resolution lock the incident, then its Runs, and retire a Job only with `SKIP LOCKED`; draft-plan invalidation follows the Run locks. A claimer locks the Job, then the Run. Cancellation therefore never waits for a claimer. The claimer sees the cancelled Run afterwards and retires the Job.

Approval performs live probes before taking admission or row locks, then checks fresh persisted state again in its commit transaction. Legacy recovery proposal/read probes still hold Incident/Workload row locks during bounded live reads. An attach waiting for those rows can indirectly retain the admission lock; this existing E07 contention is not a throughput or lock-free guarantee. The full order is documented in `features/incidents/coordination.py`.

## Migration

`0006_incident_requests` is additive. It keeps all IDs, human text, evidence, resolution snapshots and admission hashes. Earlier Runs show `not_captured`. A partial unique index enforces at most one queued or collecting Run per incident. The upgrade stops without changes if existing data violates that rule. A downgrade refuses to run when automatic origins, deferred Runs or cancelled Runs/Jobs exist; restore a pre-0006 backup instead. Request questions and windows saved after the upgrade are dropped by a downgrade.

## Verification receipt

See [local request/admission acceptance](../../acceptance/e03-incident-admission.md) for PostgreSQL, browser and separate-worker-process evidence, reproduction pointers and remaining feature boundaries.
