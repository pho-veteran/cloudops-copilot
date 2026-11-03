# Persisted Work Queue Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the core server-backed search and prioritization behavior in [issue #37](https://github.com/pho-veteran/cloudops-copilot/issues/37).

**Architecture:** Extend the existing incidents list endpoint with a bounded queue projection that joins incident metadata to Run, acknowledgement and relevant E07 execution state. Reuse the shared E07 execution classifier, keep view/filter/pagination truth on the server, and have the persisted WorkQueue render the returned projection and preserve its query state in the hash URL.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/PostgreSQL, OpenAPI generation, React, TypeScript, TanStack Query, Vitest.

**Spec:** GitHub issue #37 and E03 decision D4; README calls out the tracker as the source of truth.

## Approaches

- Fetch incident, Run and execution data independently in the browser: rejected because pagination and filters would no longer reflect server truth and each row would require extra requests.
- Add a second queue endpoint alongside the incidents endpoint: rejected because it creates a parallel query contract for the same persisted records.
- Extend `GET /api/incidents` with a typed queue projection, filters/views, and a creation-time anchor: selected because it preserves the existing transport boundary and supports server-side truth in one bounded page request.

## Global Constraints

- Persisted app remains the default; do not use simulation/localStorage as API results.
- Incidents owns the queue query/projection; consume the E07-owned shared execution classifier and do not introduce another execution policy.
- Regenerate OpenAPI/TypeScript artifacts with `scripts/contracts.py --write`; do not edit generated transport artifacts manually.
- Keep API data truthful: unknown/not-provided values remain explicit; a queue view grants no mutation authority.
- Work only in the existing `gudgeon` feature worktree; do not commit or publish as part of this plan.

## Review Focus

- A resolved incident with a retained unknown/active target lock still shows the warning, even if it is absent from Blocked.
- Historical failed runs remain visible for Blocked while the newest Run is terminal or successful.
- Multiple relevant executions remain visible; selecting only the newest result must not hide an active/unknown lock.
- New rows created after the initial anchor stay out of later pages until explicit refresh resets the anchor.
- Empty/invalid URL query values canonicalize safely and query changes reset offset without losing selected incident context.

## File Map

- `services/backend/src/cloudops/features/incidents/{api.py,schemas.py,service.py}`: bounded queue filters, projection, view behavior and creation anchor.
- `services/backend/src/cloudops/features/recovery/classification.py`: SQL projection of the existing shared classifier, if needed to keep queue filters aligned with the same policy.
- `apps/web/src/app/routes.ts`: typed URL state for view, execution/run filters, anchor and existing filters.
- `apps/web/src/features/incidents/{queries.ts,WorkQueue.tsx}`: typed query, view controls, independent status axes, provenance, actors and refresh behavior.
- `contracts/openapi.json`, `apps/web/src/lib/api/generated/schema.d.ts`: generated transport artifacts only.
- `docs/foundation.md`: describe the delivered persisted queue contract and its pagination/refresh limits.

## Tasks

### Task 1: Server-backed queue projection and filters

- Add typed queue item projection including incident metadata, latest Run with actual state/capacity reason/requester, acknowledgement actor/time, and all active/unknown/unsupported execution summaries with independent lock status.
- Add server filters for Run state, execution class/state, and operational view. Define views from D4: Needs attention, Awaiting decision, Verifying, Blocked and Resolved. Blocked includes capacity-deferred queued Runs, waiting-for-quota, failed/insufficient-evidence, and relevant uncertain/manual-intervention executions; always exclude resolved incidents from Blocked.
- For all views, retain relevant active/unknown execution warnings on resolved rows; query every relevant execution rather than only the latest.
- Add an initial server creation-time anchor to `IncidentPage` and apply it to all pages. Keep deterministic ID tie-breaks and existing bounds. Refresh can request a new anchor.

### Task 2: Queue URL state and presentation

- Preserve view/filter/sort/anchor/offset/selected incident in the queue hash and reset offset when query criteria change.
- Add operational view and Run/execution filter controls. Render lifecycle, alert, Run and execution as separate axes; include real capacity deferral, requester and acknowledgment actor/time, severity provenance, and all relevant execution/target-lock warnings.
- Provide refresh that resets the creation anchor; present current filter context and no-data/no-match/error recovery distinctly.

### Task 3: Contracts and operational documentation

- Regenerate the OpenAPI and TypeScript artifacts from backend schemas.
- Update foundation queue documentation with filters/views, anchor/refresh semantics, mutable-sort limitation, and evidence boundaries.

## Execution Note

The user subsequently requested whole-branch review and full test execution. Added focused queue coverage and ran the full backend and web suites: 724 backend passed / 9 skipped; 116 web passed. Web tests were capped at three workers to avoid resource-related timeouts seen with the default worker count. Contract generation/drift, Ruff and the production build also passed.
