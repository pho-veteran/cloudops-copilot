# Versioned resource reports

Resource reports are advisory. Operator generation creates one bounded durable job
for one configured live workload. Both roles can inspect/export stored reports;
there is no apply route or recovery-plan shortcut. The model is not involved.

The versioned report contract captures the admitted workload identity and fixed
profile. Job phase, saved result and current applicability are independent. Typed
CPU/memory values distinguish a quantity, deliberate unset and unavailable advice.
CPU is stored in cores, memory in bytes. A report can disclose usable CPU alongside
excluded memory, with separate evidence, coverage and reasons for each resource.
The structured producer must provide matching source/tool/window provenance before
publishing available advice. An old runner receipt only establishes that the tool
ran: it remains insufficient or unavailable and supplies no numeric recommendation.

## API and consumer handoff

`POST /api/recommendation-reports` accepts `workload_id` and `requested_hours`
(default six, allowed range 6..24). Only an Operator can admit work. One active
versioned report per workload and the shared queue limit are checked atomically.
Unqualified history profiles remain nonavailable; allowing a duration does not
establish representative history. Endpoints, queries and flags are not user inputs.

`GET /api/recommendation-reports` returns `items`, `next_cursor` and the feature's
sanitized operating observation. Use `limit=1..100` and optional `workload_id`.
Pass the returned cursor with the same workload filter. Ordering is descending
`created_at`, then UUID, with full timestamp precision; it does not stop at the
former newest-50 boundary. Removed/replaced workload identities remain separate.
`GET /api/recommendation-reports/{id}` reads the exact selected report, including
bounded attempts, dispatch-pause reason and current/stale/unknown applicability.
Missing IDs never fall back to the latest report. Generated transport schemas own
these contracts; the report producer, coverage policy and UI consume them directly.

`GET /api/recommendation-reports/{id}/export` returns the stored JSON bytes.
New queued/running reports return `409 report_not_final`. Authoritative terminal
publication and export creation share the existing lease/permit-fenced transaction.
A losing worker cannot replace the final document. Export bytes and SHA-256 stay
unchanged across restart, later reports, source outages and workload retirement.
Read-time applicability/dispatch status are deliberately outside those bytes.
Terminal failed/nonavailable reports retain their reasons and are exportable.

Known age of at least 24 hours or known identity/template/resource drift takes
precedence as `stale`; an unavailable current observation otherwise gives `unknown`.
For a query envelope the earliest possible analysis end starts the age calculation.
Current identity checks read recent retained observations, never run KRR or change
the cluster. Feature health separately describes the latest qualified report,
worker freshness and configuration; historical success alone is not current health.

## Migration cutover

Upgrade API, worker and web together. Rolling old/new report producers are not a
supported cutover. Keep the original application backup and dispatch audit.

1. Stop API admission and every old worker before migration. Use the existing
   trusted dispatch administration procedure to enter recovery and retire its
   generation; already admitted network calls may finish but cannot publish under
   the retired permit. Keep recovery execution disabled.
2. Apply migration `0017_recommendation_reports`. It locks the report/job/attempt
   tables, snapshots each legacy export using the prior serializer and PostgreSQL
   JSONB value order, and cancels queued/running legacy jobs while clearing leases.
   Original report IDs, state, reason, payload and timestamps remain intact.
   `migration_disposition` separately records legacy cutover versus interrupted
   work. A frozen queued legacy snapshot describes cutover history, not live work.
3. Start the upgraded services and verify exact legacy exports against the cutover
   receipt. Resume only the intended dispatch classes with the existing trusted
   CLI; interrupted legacy jobs require an explicit new report. A retained legacy
   row cannot be reclaimed by the new recommendation handler.

Legacy byte compatibility is with the baseline serializer **at cutover**, not every
older download of a report that was still running. Existing snapshots are never
reinterpreted as qualified versioned advice. Downgrade refuses to discard any
version-two report; preserve the schema/data rather than forcing a downgrade.

The source qualification seam below is implemented independently from numeric KRR
publication. Actual pinned-tool normalization, the measured six-hour cohort and
complete report UI/export journey retain their E08 acceptance gates.

## Contract-slice evidence

The [R1 receipt](evidence/e08-r1-contracts.json) records actual PostgreSQL/API/worker
restart, Viewer denial and exact immutable API bytes over a labeled controlled
fixture. Edge observed [queued export disabled](images/e08-r1-pending-export-320.png)
and Viewer reads at [320px light](images/e08-r1-viewer-320-light.png) and
[390px dark](images/e08-r1-viewer-390-dark.png). Native download completion remains
unproven; partial browser-file bytes or a successful API export do not satisfy it.
No fixture result is a live KRR or six-hour history qualification.

## UID-bound source qualification

`SourceReader.read` and `qualify` provide R3 a typed eligibility receipt, not numeric
recommendations. They reuse `KUBE_READ_CONFIG`, `PROMETHEUS_READ_CONFIG` and E02's
retained `WorkloadObservation`/`ObservationAttempt` records. No model, writer,
second history store, caller-supplied query or Kubernetes mutation is involved.

Set `RECOMMENDATION_SOURCE_POLICY` to a trusted private JSON file:

```json
{
  "schema_version": 1,
  "cluster_fingerprint": "<the committed 64-character cluster fingerprint>",
  "operator_managed_uids": ["<exact Deployment UID>"],
  "resource_managers": ["<reviewed manual resource-field manager>"]
}
```

This is an operator assertion about resource automation for these exact UIDs,
separate from ordinary managedFields. Review the actual deployment's management
before setting it. Unknown resource managers, VPA or unsupported HPA metrics require
manual review; CPU/memory HPA Resource/ContainerResource targets exclude their
corresponding resource. GET `/apis` must establish VPA API absence; forbidden reads
and arbitrary 404s never mean absent. Before/after controller checks also inspect
the namespace's Deployment selectors and selected Pod shapes. Overlap, unsupported
selectors, init/ephemeral/Pod-level resources or differing Pod resource/image fields
prevent usable advice. No new Kubernetes permission beyond the existing scoped
reader is required: get the exact Deployment, list namespace Deployments/Pods/HPA,
read API discovery, and list namespace VPA if its v1 API is discovered.

The fixed metric catalog is `container_cpu_usage_seconds_total`,
`container_memory_working_set_bytes`, `kube_pod_info`, `kube_pod_owner`,
`kube_pod_created`, `kube_pod_completion_time`,
`kube_pod_container_state_started`,
`kube_pod_container_status_restarts_total`,
`kube_pod_container_status_last_terminated_reason`,
`kube_pod_container_status_last_terminated_timestamp` and
`kube_replicaset_owner`. Each query is an instant GET to `/api/v1/query` containing
`METRIC{namespace="<escaped configured namespace>"}[<raw-window-seconds>s]`, evaluated
at the recorded raw-input end. A one-second overfetch is discarded before coverage.
These raw range vectors retain actual scrape timestamps; `query_range` lookback
repetition cannot manufacture covered bins. The native UID shapes are documented
in [KSM v2.20.0](https://github.com/kubernetes/kube-state-metrics/blob/v2.20.0/docs/metrics/workload/pod-metrics.md).
KSM ReplicaSet names alone do not establish Deployment UID: the retained E02 chain
must independently bind each Pod UID and ReplicaSet UID to the exact target.
New E02 observations retain only a SHA-256 of the full canonical Deployment Pod
template, covering environment, command, probes and other template changes without
storing their values. Reports capture it as `pod_template_digest`; the existing
`template_digest` keeps its selected name/image/CPU/memory meaning for compatibility.
R2 requires a matching full digest throughout the actual window and before/after
collection. Older observations with no full digest remain unproven; no historical
digest is backfilled from today's template. A qualifying cohort therefore needs
the updated observer running before its warmup begins.
Stop/update older API, worker and observer processes before publishing the new
metadata: older strict DTO readers do not accept the additional JSON field. New
readers accept old rows as unproven; stored report export bytes remain untouched.

The collector has a 45-second total deadline, per-phase 5/8-second timeouts, no
redirects/proxy inheritance, trusted HTTPS CAs, at most 23 GETs, 8 MiB per response,
256 series per metric and 200,000 raw samples in total. It supports the qualified
six-hour profile plus a bounded input/envelope margin, not 24-hour qualification.
Oversized, malformed, nonfinite, negative, empty, duplicate-attribution or denied
inputs remain nonavailable. Credentials and raw provider bodies are never part of
the receipt. Relevant configuration/query/history digests bind its provenance;
credential rotation alone does not change the source binding.

Coverage uses distinct one-minute bins for **each** independently established
Pod UID/container lifetime: ratio at least 95%, longest gap at most 300 seconds
including edges, and newest sample within 120 seconds of that lifetime's end.
CPU additionally needs real 75-second rate support; a newly created container's
unsupported opening bins remain uncovered. Unknown lifetime edges retain the full
denominator and explicit reasons. Pod creation/completion and container start data
are independent of usage presence. One replica cannot compensate for another.
Memory also requires covered restart samples and matching successful E02 attempts;
failed/missing attempts, resets, unexplained in-window increments and unknown
container lifetime suppress it. Proven in-window OOM suppresses all numeric memory
advice. An OOM reason without trustworthy timing remains insufficient rather than
being labeled a proven recent event. Sampled evidence cannot prove every event was
observed.
Unresolved in-window container state/reason also suppresses memory, including the
first termination before its restart counter increases. A proven clean completed
lifetime remains distinct. After pre-window completed Pods are filtered out, each
resource still needs at least one applicable Pod/container evidence interval.

E02 retention now keeps at most 1,024 observations and 1,024 attempts per workload,
still bounded by seven days. At nominal 30-second cadence this covers more than
the 6h08m lab cap, but dense manual reads can evict the required window: readiness
must inspect actual rows and failed-attempt gaps. The history API page limit stays
100. Worst-case full observation payloads are 64 MiB per workload (6.25 GiB over
100 identities), excluding database/index overhead; actual lab storage/headroom
must be measured before a cohort. Cleanup remains active and finite.

The deterministic fixture matrix and PostgreSQL cleanup checks prove these policy
branches. The [R2 scoped source-smoke receipt](evidence/e08-r2-source-smoke.json) exercises the installed reader against
the owned K3s/Prometheus; its short history remains insufficient. It is not a
numeric recommendation, representative six-hour run or integrated E08 pass.
