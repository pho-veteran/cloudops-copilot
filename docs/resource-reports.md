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

The coverage/ownership-history producer, actual pinned-tool normalization, measured
six-hour cohort and complete report UI/export journey retain their E08 acceptance
gates. Source inspection or a successful CLI invocation does not complete them.

## Contract-slice evidence

The [R1 receipt](evidence/e08-r1-contracts.json) records actual PostgreSQL/API/worker
restart, Viewer denial and exact immutable API bytes over a labeled controlled
fixture. Edge observed [queued export disabled](images/e08-r1-pending-export-320.png)
and Viewer reads at [320px light](images/e08-r1-viewer-320-light.png) and
[390px dark](images/e08-r1-viewer-390-dark.png). Native download completion remains
unproven; partial browser-file bytes or a successful API export do not satisfy it.
No fixture result is a live KRR or six-hour history qualification.
