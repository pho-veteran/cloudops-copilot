# Workload observation

The dedicated observer reads configured live Deployment identities through the host-configured Kubernetes reader and persists sanitized rollout observations. Inventory, detail and history APIs read PostgreSQL only. **Refresh displayed data** refetches that saved projection; it does not issue Kubernetes requests. Viewer and Operator can inspect the same evidence. Only Operator can start the existing manual investigation.

This implementation targets [E02 task #27](https://github.com/pho-veteran/cloudops-copilot/issues/27). Active requirements remain in [E02 #5](https://github.com/pho-veteran/cloudops-copilot/issues/5). The direct-read producer also supports [baseline v1 confirmation and read-only recovery context](workload-baselines.md). Application/Prometheus health and new executable recovery actions remain unavailable. Existing baseline v0 and E07 scale semantics remain unchanged.

Persisted rollout observations are E02 records; they are not investigation metric evidence. The optional Prometheus metric adapter and the read-only Run timeline that consumes observation timestamps are documented in [scoped Prometheus metric evidence](prometheus-metrics.md).

## Start and stop

Use the existing [local lab](e01-local-lab.md) to establish the scoped reader. Never mount writer, dispatcher, KRR or executor-authority material into the observer. With the integration overlay configured:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml --profile observation up -d --build api worker https observer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml logs --tail 50 observer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml stop observer
```

The optional `observation` profile leaves the fixture-only base stack runnable without Kubernetes. The observer has no inbound port. Its only operational dependencies are PostgreSQL, the configured scope and the fixed read-only connection. Missing/expired reader configuration produces classified unavailable observations and bounded backoff; it does not make the API unready or turn fixtures into live data.

Stop the stack when it is unused and preserve named database/K3s/authority volumes. Starting it again does not refresh old observation timestamps. API and UI age the previous evidence before a new successful poll. Any later AWS deployment remains on-demand in `us-east-1`; this feature provisions no cloud resources or model service.

## Health, coverage and age

Health describes Kubernetes rollout evidence: `healthy`, `progressing`, `degraded`, `scaled_to_zero` or `unknown`. Application traffic, referenced configuration contents and external effects remain unverified. A complete collection can be degraded or progressing; a successful collection is not proof of a healthy application.

Pods count only after controller-owner traversal from Pod to ReplicaSet to the expected Deployment UID. Labels alone are insufficient. The current ReplicaSet must match the full Deployment PodTemplate, ignoring only the controller-added `pod-template-hash` label. Typed list items may omit their own TypeMeta; the reader validates the enclosing list kind/version. Terminal/terminating Pods are excluded from positive coverage, and active old-ReplicaSet Pods prevent healthy rollout. Regular/init containers, native sidecars, missing status and known current failures are evaluated separately. Scaled-to-zero cannot establish a healthy baseline.

Missing controller or Pod/source evidence remains nullable and yields partial coverage. Once a valid controller `observedGeneration` is present, omitted Deployment replica counters represent encoded zero values; they do not erase complete unready/crash-loop evidence or its restart history. Explicit null/invalid counters remain incomplete. A read-window change, incomplete pagination or ambiguous current ReplicaSet cannot establish healthy state.

Freshness is separate from health. Last complete success at age **<=60 seconds** is fresh; **>60 seconds** is stale. A newer denied/partial/failed refresh immediately makes effective current health unknown even while the prior success is young. It remains historical until a later complete observation. A pending retry does not revive health invalidated by a previous failure. Pending reads lose positive health at their persisted deadline, including when a process dies. Browser badges also age and downgrade without waiting for another server response.

Last attempt, success and failure are separate timestamps. `permission_denied`, `target_changed`, `attempt_timeout`, `lease_expired`, `coverage_changed`, `status_missing`, `budget_exhausted` and other allowlisted codes are safe classifications, not provider messages or root-cause diagnoses. An observation 404 never retires or adopts an identity: scope synchronization owns admission. Historical UUIDs remain readable and cannot create new investigations after retirement.

## Bounds and recovery

| Bound | Policy |
|---|---|
| Scope | At most 100 configured live identities; fixtures/inactive rows are not polled |
| Poll / stale | 30-second due interval / stale after 60 seconds |
| Concurrent leases | Global maximum 4 across processes, at most 3 scheduled; one per workload, one slot reserved for direct consumers |
| Attempt / lease | 12-second read deadline / 15-second lease; no unbounded renewal |
| HTTP | GET only, fixed HTTPS host and paths, trusted CA, no proxy inheritance or redirects, 4-second phase timeout within total deadline |
| Collection | Page size 100, at most 200 ReplicaSets and 500 Pods per namespace/read window; at most 9 GETs |
| Payload | 2 MB per response, no compressed responses; sanitized observation <=64 KiB; at most 100 owned Pods/32 containers each |
| Failure retry | 30, 60, 120 seconds plus at most 3 seconds jitter; no recurring product Job |
| Retention | At most 100 complete observations and 100 attempt records per workload, also limited to 7 days; one bounded latest partial payload |
| Database | Separate observer pool of 2, no overflow; 3-second connect, 2-second pool/statement and 1-second lock timeout |

These are policy settings, not measured availability SLOs. Namespace lists that exceed the bounds produce incomplete coverage. Overdue scheduling is visible as poll lag/budget exhaustion. The scheduler does not stamp unattempted workloads fresh. Startup spreads initial due times within 30 seconds and subsequent selection favors oldest due identities.

Claims and publication use short independent transactions; no HTTP runs under a database transaction. SQL runs off the async HTTP cancellation loop. A database advisory lock enforces global lease admission separately from incident admission. Final publication checks the current active identity, source, observation epoch, token, sequence and deadline. Scope sync advances the epoch even for same-UID re-add, preventing a late read from an earlier authority window. A late token cannot clear another producer's lease.

Legacy UID-pinned records keep their existing authority rules. The reader verifies continuity of the host server/CA configuration around a read; this continuity hash is **not** an operator-established v2 cluster fingerprint. For v2 scope, the observation claim also captures the operator-established pin from the committed scope binding, and the reader verifies it before any HTTP request. Both legacy and v2 scope publication advance observation epochs, including unchanged UIDs and retire/re-add cycles. The additive observation migration `0005_workload_observations` follows `0004_workload_scope` in one Alembic chain and preserves populated scope authority. Keep the reader policy separate from the UID-pinned executor allowlist; discovery never enables a writer.

On database failure, attempts back off without logging connection strings or provider bodies. Expired leases lose publication authority and are swept. Shutdown cancels active HTTP; an interrupted claim/transaction can leave a lease that expires normally. Count/age cleanup also handles failure-only and retired histories. API reads suppress out-of-retention payloads even while the observer is stopped. Minimal timestamps/reasons may survive expired full evidence.

## Direct observation consumer

`features.workloads.observation_service.observe_direct(workload_id, purpose)` is the internal async entry point for `baseline_candidate` and `baseline_confirm`. It acquires a real lease and performs fresh reads through the same reader/evaluator. Busy returns `budget_exhausted`; unavailable/partial results do not fall back to dashboard cache. No public endpoint accepts a destination or raw query.

Unknown, inactive and fixture identities return classified failures without an attempt or network read, including when observation capacity is full. These are not busy/retryable live targets. Optional declared CPU/memory fields remain absent in serialized extraction when undeclared; no null or zero quantity is invented.

Consumer example (input acquisition only, not baseline confirmation):

```python
from cloudops.features.workloads.observation_service import observe_direct


async def read_candidate_input(workload_id):
    result = await observe_direct(workload_id, purpose="baseline_candidate")
    if result.outcome != "complete" or result.observation is None:
        return None, result.reason
    return result.observation, None
```

The [typed schemas](../services/backend/src/cloudops/features/workloads/observation_schemas.py) and [controlled examples](../services/backend/tests/observation_fixtures.py) define fields independently of the public display DTO. The caller still must enforce healthy/complete identity and the two-observation stability/confirmation policy owned by #28. Database availability errors propagate to the composing entrypoint's existing error handling; they never authorize a cached candidate.

The version-1 DTO contains identity/epoch, read window, opaque Deployment/collection resourceVersions, generation/counters, current ReplicaSet and owned Pod/regular/init evidence, whitelisted `template_fields`, `config_references`, coverage, health, reasons and limitations. Sanitization precedes persistence, internal return and comparison. Environment literals, command/args, annotations, provider/Pod/Event messages, Secret/ConfigMap contents and credentials are excluded. Reference extraction preserves source identities and quantity strings; unsupported reference forms explicitly limit coverage.

#28 owns canonical baseline envelopes/digests, fresh candidate/confirm checks and read-only recovery handoff. It must copy the two required immutable summaries into its baseline rather than depend on history that expires. It must not implement another extractor, use cached UI health as stability evidence, change `snapshot()`/`healthy()` for E07, or turn v0 baselines into v1 by relabeling them.

Restart deltas require comparable Pod UID/container kind/name sets and nondecreasing counters. Replacement or missing sets return `coverage_changed` and a null delta. Observed changes compare the same sanitized fields/references and label compared IDs, times and generations. They do not assert unobserved rollout history, unchanged referenced content or a diagnosis.

## APIs and browser journey

- `GET /api/workloads` and `GET /api/workloads/{workload_id}` include one shared freshness-aware summary; inventory batches state/evidence reads.
- `GET /api/workloads/{workload_id}/observation` returns the persisted current/partial projection, labeled historical evidence and last-complete comparison. Private template/reference extraction is excluded from public detail.
- `GET /api/workloads/{workload_id}/observations?limit=25&cursor=...` returns retained complete summaries ordered by observation time/UUID with a bounded cursor.
- `GET /api/incidents?workload_id=<UUID>&limit=25&offset=0` filters exact identity before pagination. Omitting the filter retains the existing queue behavior. Offset pagination is not an insertion-stable snapshot.

All reads reuse E01 authentication and session isolation. Unknown UUIDs return 404, malformed IDs/cursors return validation errors. A replacement UID never inherits the old UUID's incidents or baselines. Workload context deep links use only `#/workloads?workload=<UUID>`. Related incidents open persisted incident IDs, and investigation pages retain the existing exact run selector. Manual intake preloads an exact selected workload even outside the first inventory page; a retired selection cannot be submitted or replaced silently.

## Migration and verification

The additive observation migration creates three feature tables and a default-zero workload observation epoch. It does not backfill health or rewrite incident/session/baseline data. Repeat upgrade and metadata checks are required; coordinate a single head against current main. Downgrade removes observation history/state/attempts and the epoch, so retain a backup if that evidence is needed. Do not use downgrade to bypass a failed upgrade.

Run the repository's locked backend PostgreSQL suite, Ruff checks, generated-contract/drift checks and web tests/build. Tests use a separate database ending in `_test`. Controlled reader/clock fixtures prove policy cases; real TLS cancellation tests use OpenSSL and a local drip-feed server. They do not replace K3s, browser or production-image evidence.

For the fixed lab probe, create a separate `cloudops27_lab` database, configure host `DATABASE_URL`, loopback `WEB_ORIGIN`/`COOKIE_SECURE`, and start the existing K3s service. Then run:

```sh
uv run --project services/backend python scripts/probe-workload-observation.py prepare --scope-size 100
# Host reader and legacy scope files are written under ignored tmp/observation27/.
# Set KUBE_READ_CONFIG and WORKLOADS_FILE to those files for the isolated host lab.
# Start the observer, API and browser using the documented locked runtime.
# Stop scheduled polling before the bounded mutation probe so comparisons are deterministic.
uv run --project services/backend python scripts/probe-workload-observation.py verify
uv run --project services/backend python scripts/probe-workload-observation.py cleanup
```

The probe prepares labeled `scope27-*` Deployments only, using one ready, one unready and remaining zero-replica targets. It preserves the original checkout UID/spec and writer policy; cleanup verifies them and removes only matching fixture names. It refreshes only existing reader/default service-account tokens for the local test. Keep tokens and raw inspection output out of receipts. Record final commit/environment, measured budget, actual browser journeys and remaining gates separately from fixture tests.

## Verified implementation candidate

[PR #32](https://github.com/pho-veteran/cloudops-copilot/pull/32) contains this implementation. Receipts identify the source commit actually exercised; later test/evidence-only commits do not imply that a different runtime was tested.

| Evidence | Covered behavior |
|---|---|
| [Live observations and persistence](evidence/observation27-live.json) | Owned Pod exclusion, restart/replacement, denied reads, selected-field/reference changes, absent quantities, sanitization, normal/native init containers, same-UID authority fencing, UID history and actual database/K3s restart |
| [Budget and process isolation](evidence/observation27-budget.json) | Real 100-workload mixed scope, measured read windows, concurrent host/Compose observers, reader-only mounts and actual approved recovery while three TLS reads were blocked |
| [Browser journeys](evidence/observation27-browser.json) | Exact page-two preselection, related pagination and selected run, original human input, stale/recovered views, revoked-session intent clearing, Viewer controls, keyboard focus and 320/390/1280 layouts |
| [Validation](evidence/observation27-validation.json) | Source-bound Linux CI, PostgreSQL/migrations, contracts/drift, standard production-image HTTPS smoke and runtime-only dependency import |

Screenshots: [fresh recovered context at 390](evidence/observation27-fresh-390.jpg), [stale historical context at 390](evidence/observation27-stale-390.jpg) and [Viewer dark at 320](evidence/observation27-viewer-320-dark.jpg). The 1280px check records DOM overflow geometry; its scaled in-app capture was omitted.

The budget population is retained complete windows, including bounded fixture exercises; failed/partial reads are reported separately. It does not establish an availability SLO or 100 healthy applications. The blocking observer transport was a controlled real TLS server; product recovery used the actual K3s executor, explicit plan approval and a separately approved restoration. The original checkout UID/spec and writer policy were preserved, and the executor was quarantined afterward. Kubernetes acknowledgement remained manual/inconclusive application verification.

Browser receipts use loopback HTTP. HTTPS protocol smoke and the standard Dockerfile build passed on CI; no new-feature HTTPS-browser or AWS deployment is claimed. Local Compose live execution used a cached pinned image with read-only current-source mounts and is distinguished from the clean CI build. Full E02 integrated baseline/recovery acceptance remains with #28/#29 and the parent EPIC.
