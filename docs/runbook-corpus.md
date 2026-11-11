# Versioned runbook corpus

This repository owns the reviewed release corpus for E05. It is source-controlled Markdown plus `runbooks/catalog.json`; it is not an index, retrieval service, browser editor or recovery executor. The release check requires exactly five diagnosis guides and three recovery-template guides.

The current corpus was reviewed against source revision `6c8eea37e982f3dc6de2755e7a482eb16aaf1f16` on `2026-11-11`. That provenance records the source review; it does not claim independent human approval or runtime recovery.

## Files and contract

`runbooks/catalog.json` is `RunbookCatalog/v1`:

- Exactly `schema_version` and `documents` at the top level. Unknown fields are rejected.
- Each document has a stable lowercase `document_key` (maximum 64 characters), explicit string `version` (maximum 64), English `title` (maximum 200), repository-relative `source_path` under `runbooks/` (maximum 256), `guide_kind`, unique `tags`, `applicability` and `provenance`.
- `applicability.kind` is `Deployment`. `symptom_families` uses the E04 family IDs. Empty `symptom_families` means generally applicable; null or missing applicability is invalid. Diagnosis guides have an empty `recovery_templates` array; recovery guides declare at least one of the three template IDs.
- `provenance.source_revision` is the reviewed application commit and `reviewed_at` is the actual review date. Do not supply approval, digest, chunk, index or generation fields; those are importer-owned by #59.
- The release corpus uses these stable keys: `crashloop-startup`, `oom-restarts`, `traffic-latency-errors`, `pending-unschedulable`, `image-pull`, `restore-deployment-template`, `set-memory-profile`, `scale-deployment`.

The corpus author-side checker is `scripts/check-runbook-corpus.py`. It uses only the Python standard library and validates the manifest path before reading it, then reads validated source files. It rejects non-object catalogs in both modes, duplicate JSON keys, unknown/imported fields, malformed types, path escapes, symlinks/reparse traversal, invalid UTF-8, broken local links, oversize input and release-coverage gaps. Manifest and document reads are bounded at I/O as well as checked before and after reading; non-regular files are rejected. `--no-release` is available for small schema-level fixtures; release checks use the default mode. `--baseline-root <copy>` compares same-key/version content and versioned metadata for the controlled update drill.

## Bounds

The parent D1 intake bounds are 100 documents, 256 KiB per file and 8 MiB for the corpus. The release corpus is eight files and well below those caps. The checker also reports sections over the 4 KiB editorial target; that is an authoring signal, not an importer chunk acceptance. #59 owns deterministic 4 KiB heading-aware chunking and its 200-chunk document cap.

Markdown must remain inert-readable: text, headings, lists and static links only. Do not add HTML, scripts, external images, executable patches, raw logs, credentials or Secret/ConfigMap contents. Local links must exist in the repository.

## Update a guide

1. Keep the stable key. Choose the owning diagnosis family or recovery template and re-read the linked feature documentation and owning issue. Do not add a recovery template to Pending or ImagePull.
2. Change the prose, tags, applicability or provenance. Increase `version` for any content or versioned metadata change, including title, source path or review metadata. Never overwrite the same key/version with different bytes or canonical metadata.
3. Use a real reviewed source commit SHA and a real ISO review date. Do not claim future implementation support, an independent reviewer or an approval that did not happen.
4. Run the checker and focused tests:

```sh
uv run --project services/backend python scripts/check-runbook-corpus.py --root . --json
uv run --project services/backend pytest -q services/backend/tests_unit/test_runbook_corpus.py
uv run --project services/backend ruff check --config services/backend/pyproject.toml scripts/check-runbook-corpus.py services/backend/tests_unit/test_runbook_corpus.py
uv run --project services/backend ruff format --check --config services/backend/pyproject.toml scripts/check-runbook-corpus.py services/backend/tests_unit/test_runbook_corpus.py
git diff --check
```

5. For a version drill, use a temporary baseline copy. Run the checker with `--baseline-root <copy>`. A same-key/version content or metadata change must report a conflict; increasing the version permits the revision while the original bytes remain unchanged. The content digest changes only with content bytes, while versioned metadata has its own digest. Do not put a temporary revision fixture into the release catalog.
6. Review the rendered text and all local links. The content review record belongs under `docs/evidence/knowledge58/`; a green checker does not prove prose quality or recovery eligibility.

## Version and digest rules

The checker computes a SHA-256 over exact UTF-8 file bytes and a separate author-side canonical metadata digest. That metadata digest is a comparison aid, not a claim of importer compatibility. #59 owns stored content/metadata hashes, immutable document-version rows, chunks and generation identity. Moving a file or changing a logical key is a versioned metadata change; paths are provenance, not identity.

The same `(document_key, version)` with different content or metadata is rejected. A new version is required. A new version does not rewrite the prior Git bytes or any later stored reference; #59 must retain historical rows/chunks. This corpus does not provide database retention proof.

## Review and handoff

The content review scope is recorded in the release receipts with the exact source revision, checker results and manual walkthrough. If one person performs the review, record that limitation rather than inventing an independent reviewer. The corpus is ready for import only as a validated input; #59 owns activation, CLI/status and immutable index behavior. #60 owns retrieval, applicability, Run pinning and `KnowledgeRef`; #61 owns persisted Knowledge/Run UI; #62 owns the frozen relevance set and integrated acceptance.

Pending/ImagePull are manual-only. Restore guidance describes baseline context and `restore_not_implemented`, not an executable restore. Memory-profile guidance documents an unimplemented API; scaling guidance documents the bounded foundation `scale_deployment` path and its approval/verification limits. A runbook hit is methodology, not a diagnosis, execution approval or telemetry.
