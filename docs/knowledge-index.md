# Immutable runbook index

The Knowledge feature turns a reviewed `RunbookCatalog/v1` snapshot into immutable PostgreSQL document versions, deterministic chunks and generation membership. The browser can read the active catalog and exact historical versions/chunks; only a trusted maintenance CLI can capture or activate a corpus.

## Contract and boundaries

- API routes are protected `GET` operations: `/api/knowledge/documents`, `/api/knowledge/documents/{document_version_id}`, `/api/knowledge/chunks/{chunk_id}` and `/api/knowledge/index-status`.
- Both Viewer and Operator roles can read. Anonymous and expired sessions are denied. There is no HTTP upload/index endpoint and no browser filesystem authority.
- Exact reads are by immutable IDs and do not fall back to the current version. Retired versions and chunks remain readable.
- Catalog search is PostgreSQL English full text over the current generation, ordered by weighted relevance (title, headings, body) with stable key/version/ID tie-breaks before pagination. Chunk projections include the document title on every chunk. It is not a diagnosis, Run selection, semantic retrieval or execution approval. Run-derived retrieval and pinning belong to #60.
- Corpus content is stored as exact UTF-8 bytes. The API returns inert text; no Markdown/HTML is executed.
- No automatic startup indexing, file watcher or background job is used.

## Trusted index command

From the repository root, with the database migrated and `DATABASE_URL` configured:

```sh
uv run --project services/backend python -m cloudops.entrypoints.cli knowledge-index --root . --json
uv run --project services/backend python -m cloudops.entrypoints.cli knowledge-status --json
```

`knowledge-index` acquires the common session-level PostgreSQL writer guard **before** reading source files. It captures one bounded snapshot, validates it, computes deterministic chunks and hashes, and commits immutable rows plus the active-generation pointer in one short final transaction. Concurrent writers fail fast with `index_busy`; the prior good generation remains readable. A process crash leaves an orphan attempt that the next successful writer reconciles as interrupted.

Outcomes are `indexed`, `unchanged`, `reactivated`, `index_busy` or `failed`. Failure and busy invocations exit non-zero. `--json` writes one receipt to stdout; diagnostics and safe failure messages go to stderr. No database URL, source body or absolute host path is emitted.

`knowledge-status` is read-only and never scans the source tree. It reports `not_indexed`, `updating`, `degraded` or `ready`, the active generation, the latest accepted attempt, last success/check, retained history counts and read/search availability. A missing source tree does not make previously stored versions unavailable.

## Corpus updates and history

- The manifest is `runbooks/catalog.json`; source paths must be relative Markdown files under `runbooks/`.
- The importer accepts conformant corpora under the D1 bounds and does not apply the release-only eight-guide rule or author-side local-link checks. Use the #58 checker separately for release-copy review.
- Repeat the same source/configuration to get `unchanged` and reuse document/version/chunk/generation IDs.
- Change content or versioned metadata only with a new explicit `version`. The same `(document_key, version)` with different bytes/metadata is rejected against all retained history.
- Removing a document from a new snapshot changes current membership only; prior versions, chunks and generations remain stored. There is no retention/purge command in #59.

## Container overlay

The backend image intentionally does not copy `runbooks/`. Use the read-only overlay for a trusted maintenance invocation:

```sh
docker compose -f compose.yaml -f compose.knowledge.yaml build migrate
docker compose -f compose.yaml -f compose.knowledge.yaml run --rm -T --no-deps migrate \
  python -m cloudops.entrypoints.cli knowledge-index --root /workspace --json
```

The API and worker do not mount the corpus and do not re-read it at startup.

## Local lifecycle acceptance

`scripts/probe-knowledge-index.py` checks authenticated read routes without changing the catalog. For process interruption and restart acceptance, use `scripts/probe-knowledge-lifecycle.py` with the backend Python environment against a newly migrated, empty, owned loopback database ending in `_test`. Configure `DATABASE_URL`, `COOKIE_SECURE=false` and a valid loopback `WEB_ORIGIN` in the environment; never pass credentials as command arguments.

The lifecycle probe requires an owned PostgreSQL container labelled `cloudops.task=pr64-review` and an unused Knowledge catalog. It provisions disposable accounts, indexes the release corpus and temporary version/update/remove fixtures, checks both roles through HTTP, interrupts owned CLI processes at controlled barriers, and restarts its API and the named database container. It refuses other container labels and nonlocal/non-test database URLs. Optimized Python execution is rejected before lab access so ownership and acceptance assertions remain active. Run it without other tests or clients sharing that container because the database restart affects all databases in it.

```sh
uv run --project services/backend python scripts/probe-knowledge-lifecycle.py \
  --container <owned-container> --output <sanitized-receipt.json>
```

Temporary sources and owned API/CLI processes are cleaned up. The database and container remain available for inspection and must be stopped by the lab owner when finished. The probe does not validate UI, Kubernetes recovery or semantic retrieval.

## Limits

- Chunker version `2` preserves heading ancestry even when heading levels are skipped and creates exact slices of at most 4 KiB and 200 chunks per document. Search/ranking configuration version `2` includes corrected chunk title weighting and relevance ordering. Reindexing rebuilds a compatible generation without changing stored document versions or old chunk identities. The current release corpus has small sections, so synthetic tests cover long-section and UTF-8 boundary splitting.
- The first use of PostgreSQL `tsvector`/GIN is part of migration `0010_knowledge_index`; no PostgreSQL extension or model provider is required.
- Historical rows grow with every accepted new version/generation. Check `knowledge-status` retained counts and storage before operational cleanup; deleting Knowledge history is not part of this feature.
- This feature does not establish release relevance, live recovery, AI diagnosis or automatic execution. Those remain with #60-#62 and later EPICs.

## Installed E05 acceptance

Use a new disposable Compose project for `scripts/probe-knowledge-acceptance.py`, with a corpus copy outside the checkout. It refuses non-loopback origins, unowned containers, non-test databases, live collection and optimized Python. The probe provisions only synthetic accounts, issues the fresh-install dispatch permit and mutates only the disposable corpus. The overlay uses separate images, loopback ports 8012/5182 and `cloudops_knowledge62_test`. Compose must support `!override` (tested with 5.6.1).

With the locked backend Python environment available, choose one disposable password of at least 12 URL-safe characters:

```sh
export KNOWLEDGE_ACCEPTANCE_CORPUS="$(mktemp -d /tmp/cloudops-knowledge62-corpus.XXXXXX)"
cp runbooks/* "$KNOWLEDGE_ACCEPTANCE_CORPUS/"
read -rs -p 'Disposable lab password: ' KNOWLEDGE_ACCEPTANCE_PASSWORD
KNOWLEDGE_ACCEPTANCE_ENV="$(mktemp)"
printf 'POSTGRES_PASSWORD=%s\nWEB_ORIGIN=http://127.0.0.1:5182\nCOOKIE_SECURE=false\nCOLLECTION_SOURCE_MODE=development_fixture\nCOLLECTOR_DELAY_SECONDS=0\n' \
  "$KNOWLEDGE_ACCEPTANCE_PASSWORD" > "$KNOWLEDGE_ACCEPTANCE_ENV"
docker compose --env-file "$KNOWLEDGE_ACCEPTANCE_ENV" -p cloudops-knowledge62 \
  -f compose.yaml -f compose.knowledge-acceptance.yaml up -d --build --wait api worker web

knowledge_probe() {
  printf '%s\n' "$KNOWLEDGE_ACCEPTANCE_PASSWORD" | \
    uv run --project services/backend python scripts/probe-knowledge-acceptance.py "$1" \
      --compose-file compose.yaml --compose-file compose.knowledge-acceptance.yaml \
      --env-file "$KNOWLEDGE_ACCEPTANCE_ENV" --project cloudops-knowledge62 \
      --corpus "$KNOWLEDGE_ACCEPTANCE_CORPUS" --base-url http://127.0.0.1:8012 \
      --web-origin http://127.0.0.1:5182 --output /tmp/cloudops-knowledge62-receipt.json
}
knowledge_probe prepare
# Check the installed catalog and Run guidance in a separate browser lab tab.
knowledge_probe mutate
# Check historical references and matching degraded Knowledge/Connections status.
knowledge_probe finish
```

`prepare` requires an unused receipt and unindexed database. It runs the frozen [16-positive/four-negative set](../scripts/fixtures/knowledge-relevance.json), both-role reads, Operator/CSRF/body boundaries and separate manual/trusted selections. The removed-current negative runs in `mutate`. Expected guide/section labels must be frozen before execution; results include exact chunk references and ranks. `finish` explicitly retries indexing, restarts PostgreSQL/API/worker/web, checks both roles against the captured selection/target/resolution hashes, creates a later Run and restores the release catalog. No ranking tuning or model/embedding call is performed by the probe. A failure retains its receipt; inspect it before changing the source or starting a separate cohort.

Stop the owned project after inspection; this removes its test database and dispatch volume:

```sh
docker compose --env-file "$KNOWLEDGE_ACCEPTANCE_ENV" -p cloudops-knowledge62 \
  -f compose.yaml -f compose.knowledge-acceptance.yaml down --volumes
```

Remove the disposable corpus/env file afterward; keep credentials out of receipts and commits. [Recorded E05 acceptance](evidence/knowledge62/README.md) separates installed-stack results, child regressions and unconfigured semantic scope.
