# Saved Run guidance

Knowledge guidance is methodology, separate from telemetry citations, diagnosis and recovery authority. It uses the existing PostgreSQL full-text index without a model or embedding endpoint.

## Manual API

An Operator explicitly calls `POST /api/incidents/{incident_id}/runs/{run_id}/knowledge` with an empty body or `{}` and the current Origin/CSRF credentials. The server uses the stored Run question, window and incident/workload UID relationship. Browser-supplied query, consumer, applicability or workload fields are rejected.

Both roles read `GET` on the same path. `not_selected` means no successful manual request exists; `saved` includes one immutable selection and its retrieval result. `unavailable` identifies missing legacy request context or an index that is not ready for its first retrieval. Infrastructure failures return typed errors and leave no successful selection, allowing explicit retry.

The first successful manual result, including `no_matches`, is retained. Repeating the request returns that selection, even after reindexing. A new Run is required for fresh manual guidance; catalog browsing still uses the active generation.

Every result names its exact generation/source digest, query/retrieval/chunker/ranking versions, scope, provenance, applicability and limitations. Queries are at most 2,000 characters; limits are 1–10 chunks with at most 32 KiB of UTF-8 content. Count/byte truncation is explicit. Failed or unfinished index updates label last-good retrieval as degraded.

Unknown family does not mean unrestricted execution eligibility. Manual requests search matching diagnosis guides across families and label applicability unverified. Pack coverage is never interpreted as a diagnosis. Recovery-template guides are omitted without explicit trusted server template context; that context is not execution approval.

## Exact references

`KnowledgeRef/v1` has `kind=runbook` and identifies the exact document key/version/ID, chunk ID/chunker version and content/metadata hashes. It cannot substitute for an E04 telemetry citation.

`GET /api/incidents/{incident_id}/runs/{run_id}/knowledge/selections/{selection_id}/chunks/{chunk_id}?document_version_id=<UUID>` checks the incident/Run relationship, selection membership, exact parent and hashes. It returns that stored document and chunk, never the current version. Reindexing, retirement and evidence expiry do not purge Knowledge history.

## Trusted consumers

The feature-owned service resolves identity from persisted Run relationships. Backend entrypoints register models through `cloudops.entrypoints.metadata`; initialized consumers control their own transaction:

```python
from cloudops.features.knowledge.guidance import pin_guidance
from cloudops.shared.database import sessions

with sessions()() as db, db.begin():
    result = pin_guidance(
        db, incident_id, run_id,
        consumer="analysis", request_identity="server-owned-analysis-request-id",
        query="server-derived evidence question",
    )
```

Trusted requests have separate selection IDs and idempotency scoped to `(Run, consumer, request identity)`. A prior manual no-match does not prevent a later trusted request from selecting relevant guidance. No E06 handler, provider call or analysis lifecycle is introduced here. E09 consumers read the existing index status; retrieval and index availability do not establish factual evidence quality.

## Persistence and checks

Migration `0011_knowledge_selections` follows the immutable index migration. It adds selections and generation-constrained chunk membership without rewriting existing incidents, Runs, evidence, catalog versions or saved resolutions. Downgrade refuses to drop saved guidance. The service only stores guidance metadata/content and does not transition incident, Run, plan or execution states.

Run `pytest -q services/backend/tests/test_run_guidance.py services/backend/tests/test_permissions.py` in the backend Python environment with `TEST_DATABASE_URL` pointing to an owned PostgreSQL database ending in `_test`. Regenerate and check OpenAPI/TypeScript through `scripts/contracts.py` after contract changes. The [installed E05 acceptance ledger](../../evidence/knowledge62/README.md) records release-corpus relevance and browser/API coverage; the [persisted UI](ui.md) uses these contracts.

## E06 and E09 handoff

E06 can call `retrieve` for a bounded read or `pin_guidance` to retain its own immutable selection in the caller transaction. Use `consumer="analysis"` and a server-owned request identity; resolve the persisted incident/Run UID/window rather than accepting browser scope. Supply a symptom family or recovery template only from trusted context. Manual unknown-family selection never establishes a diagnosis, and a manual no-match does not consume a trusted request identity. Both return `KnowledgeRetrieval/v1` with `KnowledgeRef/v1` runbook references, generation/config versions, budgets and explicit gaps. Resolve exact references and preserve their separate type when composing context; never relabel them telemetry or silently substitute current content. Final context relevance, untrusted-content handling, model egress and RCA lifecycle remain E06 responsibilities. No E06 handler is implemented by E05.

E09 can reuse `GET /api/knowledge/index-status` (`get_knowledge_index_status`) under the existing session boundary. `retrieval_mode=catalog_full_text` and `semantic_mode=not_configured` describe the installed baseline; retrieval payloads use `mode=full_text`. `last_checked_at` refers to a trusted CLI source capture, not a filesystem watcher or source-health guarantee. `degraded`/`updating` can retain readable history and a last-good generation; `not_indexed` has no first successful generation. Keep these feature-owned semantics when projecting broader health, and do not add reindex authority to a browser status client.
