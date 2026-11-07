# Stored Run comparison and citations

This slice implements [E04 task #49](https://github.com/pho-veteran/cloudops-copilot/issues/49). It consumes the persisted evidence API from #45. Kubernetes and Prometheus investigation adapters, richer collector-specific panels, AI findings and integrated E04 acceptance retain their respective owners. Viewing or comparing evidence never starts collection, calls a provider, creates a pin or authorizes recovery.

## Use the persisted workspace

1. Open an incident and select an exact Run in its history. The simulation's comparison view is separate.
2. In **Compare runs**, specify both incident and Run IDs, then choose **Compare selected runs**. IDs from older history pages work without loading every preceding page. Both signed-in roles can compare readable records.
3. Inspect the two workload identities and windows. A replacement Deployment identity is explicitly not comparable. Unrelated workloads are rejected.
4. Select a source. The source list keeps original coverage/content status and expandable evidence IDs, hashes and reasons. **Show differences only** hides unchanged semantic rows, retaining explicit unavailable states.
5. **Swap left and right** preserves the selected source and filter. Added and removed reverse direction. Neither original changes.
6. Follow an exact left/right evidence target. Its source metadata, retained content and focused citation region identify the historical observation. Return to the selected comparison using the dedicated link or browser Back.
7. **Export loaded comparison** downloads JSON for the current response/page only. The export states that scope and includes a continuation cursor when more rows exist. It is separate from the immutable incident handoff export.

Source selection is explicit: a newer, failed or cancelled Run never replaces a chosen side. Failed or incomplete Runs can still expose committed retained evidence; missing sources remain not recorded. No result is labelled RCA.

## Read API

All routes require the existing application session. Operator and Viewer can read them. Authentication failures are 401; wrong incident/Run/evidence scope is 404. No new mutation permission or role is introduced.

| Operation | Route |
| --- | --- |
| `compare_runs` | `GET /api/incidents/{incident_id}/runs/{run_id}/compare` |
| `list_citation_targets` | `GET /api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/citation-targets` |
| `resolve_citation` | `GET /api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/citation` |

Comparison requires `right_incident_id` and `right_run_id`. Both sides are checked before returning either identity. The current app permits both roles to read all incidents; this is not a new tenant/ACL implementation. Keeping both scoped checks preserves the policy seam for future authorization.

Without source parameters the response is metadata-only. For semantic details, send **both** `source_key` and `catalog_version`. Optional `changed_only`, typed `difference_status`, `limit` (1–100, default 50) and `cursor` apply to detail rows. A nonexistent selected source is 404. Catalog and payload versions are never silently merged. The UI's Difference status filter is retained in the URL; changing it clears the differences-only toggle to avoid conflicting filters.

The `Compare/v1` response contains explicit side identities, questions/windows/states, sorted source metadata, semantic rows with exact `CitationRef` values, reasons and a continuation cursor. A same-name replacement under the same cluster/namespace returns `identity_relation=replacement`, `status=not_comparable` and no value alignment. Unrelated pairs are 422. Retired workload history remains readable.

Citation shape:

```json
{
  "run_id": "<uuid>",
  "evidence_id": "<uuid>",
  "locator": {"kind": "field", "key": "v1_<64 lowercase hex digits>"}
}
```

Omitting the locator resolves the source metadata, including an expiry tombstone. An exact target request supplies `locator_kind` and `locator_key` together. Listing accepts an optional `kind` and paginates with the same 1–100 bound. Exact content still uses the existing lazy evidence-content route.

| State | Result |
| --- | --- |
| Wrong reference or exact target absent | 404; never switches to latest or nearest match |
| Payload expired/purged | 410 for target/content reads; metadata/digest remain readable |
| Source content unavailable | 409 |
| Unsupported projection or ambiguous target identity | 409 with a sanitized explicit reason |
| Incomplete/invalid locator, source pair or cursor | 422 |
| Cursor's stored comparison context changed | 409; reload the selected page |
| Serialized response exceeds the hard envelope cap | 413; inspect the existing individual source reads |

## Semantic identities and compatibility

`locator_v1` is a deterministic read projection of the already redacted persisted payload. It never rewrites payloads or hashes. Keys encode kind plus semantic identity through SHA-256; URLs contain no log text or sample values. Keys are scoped by the evidence ID. They are not array indexes, JSONPath expressions, filesystem paths or external destinations.

- State fields use semantic object paths, Pod UID, container context and condition type. Anonymous retained objects use a content identity rather than an invented historical identity.
- Events use event UID when captured, otherwise a canonical retained-content identity.
- Logs use Pod UID, container, current/previous context, captured timestamp and the retained segment digest. Named nested container wrappers carry their Pod/container identity and captured segment context into their log children; absent Pod UID never becomes an invented target. A segment is not a claimed raw log line number.
- Series use canonical retained series context/labels/units. Samples add a normalized timestamp; neither interpolation nor gap filling occurs. Finite numeric sample values compare by numeric meaning (for example, `1`, `1.0` and `"1.0"`), including duplicate-identity ambiguity checks, while previews and retained source bytes keep their original representations. Ordinary state/fixture strings are not coerced.
- Identical duplicate identities are grouped with occurrence counts. Conflicting duplicates are marked ambiguous, not arbitrarily selected. Comparison links those rows to the original source rather than pretending an exact conflicting target resolves.

Source order is source key then catalog version. Semantic rows sort by kind and semantic identity. Field previews are bounded and retain links to originals. Added/removed means observed membership in the retained snapshots; partial coverage cannot establish absence. Partial gaps use unavailable states instead of zero or a removal claim.

Numeric sample comparison requires the same workload UUID and UID, supported payload version, compatible captured windows/query metadata, known query/version/units/step/source identity and complete coverage. Missing units/provenance, conflicting sample identities or incompatible context remain not comparable. Windows remain visible even when values cannot be aligned. An inconsistent embedded Deployment UID also prevents value comparison. Correlation is never presented as causality.

Unknown historical versions retain metadata and original content when available. Known legacy fixture payloads can be inspected through their own declared version; a legacy catalog is not aligned with a standard catalog merely because a source key matches. Comparison labels legacy source coverage **Not captured (legacy)** independently of whether its retained content is available or expired.

## Persistence, bounds and concurrency

No new tables, migrations, job kind, background worker or durable diff cache are added. Existing evidence pins and retention maintenance remain authoritative. Comparing does not extend retention.

Each API request uses its own read-only repeatable-read PostgreSQL transaction, separate from session authentication. Summary reads keep deferred payloads unloaded. Detail reads load at most one source per side. No transaction spans a provider call or user interaction.

Detail/target pages have a 128 KiB item budget in addition to the row count. Individual value previews stay below 1 KiB of UTF-8; labels are bounded to 240 characters. The serialized response has a hard 256 KiB cap. Projection work remains within the retained source bounds from #45, including the existing 20,000-sample Run ceiling; there is no pairwise sample Cartesian product or unlimited string diff.

The finite [mechanical benchmark](evidence/comparison49/bounds.json) uses 25 series and 20,000 stored-shaped samples per side. Reproduce it with `uv run --project services/backend python scripts/benchmark-comparison.py`. Its measured Python time and traced memory include tracing overhead; they are not a live HTTP/collector latency or capacity guarantee.

Cursors are untrusted paging hints bound to exact IDs, source/catalog, filter, projection version and stored context signature. Authorization is repeated on every request. A source-set, pin or availability change can invalidate a cursor. An in-progress Run is a committed snapshot at read time, not a finished immutable collection.

Protected comparison/query data is cleared by the existing session boundary. It is not persisted to localStorage. URLs retain validated IDs, source/filter and opaque locator keys through reload and re-login. Citation locators stay scoped to the selected evidence: changing a comparison source or filter clears the locator, and selecting another evidence source retains the comparison return context without reusing the previous target. Duplicate or incomplete parameters are rejected by the safe route parser.

## Current and saved handoffs

The handoff panel reads references from structured snapshot metadata. It never parses rendered Markdown to grant navigation authority. A saved reference keeps its selected resolution ID and exact historical Run/evidence IDs. Missing legacy metadata remains not captured; it is not backfilled from the newest Run.

Saved snapshot JSON, frozen Markdown bytes and their existing pin ownership are unchanged. Production finding-to-evidence citations remain unavailable until the owning producer exists. Contract-fixture references stay explicitly separate from a live findings claim.

## Verification and local reproduction

The acceptance environment uses a dedicated loopback API/web and PostgreSQL lab. Kubernetes/state/event/log/series examples are explicitly **DB-seeded persisted contract fixtures**, as allowed by #49, rather than live Kubernetes or Prometheus collectors. Their incident title/description and receipts identify that proof class. Normal admission and the development worker created the Runs; controlled fixture preparation supplied source-shaped stored records before inspection. Pack coverage from that original development collection is not promoted into all-pack live acceptance.

Focused behavioral checks:

```sh
uv run --project services/backend pytest -q services/backend/tests_unit/test_citation_projection.py services/backend/tests_unit/test_semantic_comparison.py
# Configure TEST_DATABASE_URL for a disposable PostgreSQL database ending in _test.
uv run --project services/backend pytest -q services/backend/tests/test_run_comparison.py services/backend/tests/test_permissions.py
npm --prefix apps/web test
npm --prefix apps/web run build
uv run --project services/backend python scripts/contracts.py
uv run --project services/backend python scripts/test-contract-drift.py
```

`scripts/probe-comparison.py` performs read-only actual HTTP checks against explicit existing loopback lab records. Supply the existing account password on stdin, not as an argument. It verifies both retained content hashes and exact citation resolution for available sources, the wrong-scope denial, response bounds and an optional saved report hash. It never seeds data, creates a Run or calls a live source.

```sh
uv run --project services/backend python scripts/probe-comparison.py \
  --base-url http://127.0.0.1:8049 --origin http://localhost:5149 \
  --username <existing-lab-viewer> \
  --incident-id <uuid> --left-run-id <uuid> --right-run-id <uuid> \
  --resolution-id <uuid> --output <before.json>
# After the owned API/database restart, repeat with:
# --compare-with <before.json> --output <after.json>
```

Keep API/worker/web versions and generated contracts synchronized. The #46 progress component and #47/#48 producers integrate through their existing contracts; this feature does not require their whole slice to merge first. Planning/source ownership stays in E04. After acceptance, stop only task-owned services and record which disposable fixtures, accounts and volumes remain.

## Task acceptance evidence

| Deliverable | Implementation and verification |
| --- | --- |
| CitationRef validation and exact navigation | `citation_schemas.py`, `locators.py`, `comparison_service.py`, `CitationTarget.tsx`; scoped API/ambiguity tests and actual historical target navigation |
| Deterministic Compare/v1 and identity rules | `comparison_schemas.py`, `comparison.py`, `comparison_api.py`, `RunComparison.tsx`; cross-incident/replacement, ordering/Swap/status/paging tests |
| Immutable originals, coverage gaps and retention | Existing scoped readers/pin authority; metadata-only SQL assertion, actual purge-boundary test, before/after content and saved report hashes |
| Independent operation before live producers | Declared stored contract fixtures through actual API/PostgreSQL/browser; source-specific current/saved handoff links; no duplicate collector or finding producer |
| Role/session/accessible responsive consumer | Updated safe routes and permission inventory; late-focus and wrapped-hit-area corrections; Viewer/re-login, keyboard and viewport evidence |

| #49 requirement | Evidence |
| --- | --- |
| AC1: exact pair, deterministic swap, persistent URL, unchanged stored originals after restart | PostgreSQL/API comparison cases; browser export; [before](evidence/comparison49/before-restart.json) / [after](evidence/comparison49/after-restart.json) actual API+PostgreSQL restart receipts |
| AC2: missing/partial/expired/schema/legacy/failed/cancelled and wrong scope | `test_run_comparison.py`, projection/semantic tests and operation permission cases; typed states preserve metadata |
| AC3: current/saved historical references, frozen report and pin policy | API regression checks for unchanged saved bytes/pins; actual historical browser reference; saved report SHA-256 preserved through restart |
| AC4: read-only Viewer, session, keyboard/focus, responsive themes | Browser screenshots below; actual Viewer session revoke/re-login preserving the comparison; component/navigation/session tests |

The original captures below used an isolated browser because no Edge relay was connected at that time, although a native Edge window was open. Subsequent acceptance used the running Edge (Edg/156) for pair/source/filter navigation, keyboard targets, historical selections, session recovery and responsive themes. The additional legacy, nested-log and numeric-equivalence captures used an isolated managed context after relay capture/download-target failures; they show declared persisted fixtures, not live telemetry. No user browser permissions or certificate trust were changed. Reference links use a block hit area so the entire wrapped label is clickable. These are loopback HTTP UI checks; HTTPS/cookie CI checks retain their separate scope.

- [Desktop comparison](evidence/comparison49/compare-desktop.png)
- [390px swapped comparison](evidence/comparison49/compare-390-light.png)
- [320px citation, light](evidence/comparison49/citation-320-light.png)
- [320px citation, dark](evidence/comparison49/citation-320-dark.png)
- [Historical saved reference](evidence/comparison49/saved-historical-390.png)
- [Viewer comparison](evidence/comparison49/viewer-390.png)
- [Actual browser download](evidence/comparison49/browser-export.json)
- [Expired citation](evidence/comparison49/expired-citation.png)
- [Failed/cancelled empty comparison](evidence/comparison49/failed-cancelled-empty.png)
- [Owned lab cleanup and retained data](evidence/comparison49/cleanup.json)
- [Legacy coverage independent of content availability](evidence/comparison49/legacy-comparison-390.png)
- [Nested named-container log citation](evidence/comparison49/nested-log-citation-390.png)
- [Equivalent numeric encodings with retained occurrences](evidence/comparison49/numeric-equivalence-390.png)
- [Exact checkpoint-prefix citation after takeover and finalization](evidence/comparison49/integrated-checkpoint-citation-390.png)

The original [source-head Linux CI](https://github.com/pho-veteran/cloudops-copilot/actions/runs/42245597674) at `701a3c0` passed **938 backend cases with no skips**, **166 web cases**, build, Ruff, migration/contract checks and the standard-image Caddy HTTPS session smoke. The [validation receipt](evidence/comparison49/validation.json), [source binding](evidence/comparison49/source-binding.json) and [cleanup receipt](evidence/comparison49/cleanup.json) describe that historical candidate and lab, not later source changes or a currently running lab. Current PR checks bind the final integrated source separately. This slice does not close E04 or assert a live AI diagnosis.

Integrated checkpoint acceptance used the actual local worker and API with explicitly registered synthetic adapters: three committed sources remained readable during interruption, then a second attempt finalized the catalog while reusing their exact evidence IDs and hashes. The same historical citation remained resolvable, and a pre-finalization comparison cursor returned 409 rather than silently paging a changed snapshot. Fresh API and Edge reads after current-base integration also preserved the original ten retained payload hashes and saved handoff bytes; Viewer mutation remained 403 and anonymous comparison 401. The integrated citation screenshot uses the isolated managed capture context; primary interaction and keyboard navigation used Edge. This is persisted fixture/consumer proof, not live Kubernetes, Prometheus or AI acceptance.
