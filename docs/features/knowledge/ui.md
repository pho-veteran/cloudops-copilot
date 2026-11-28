# Persisted Knowledge UI

Sign in to the default app and open **Knowledge**. This route uses the authenticated catalog and exact-read APIs; the `?simulation=1` demo remains separate. No model or embedding endpoint is required.

- Search the current generation with up to 2,000 characters and 16 comma-separated tags. Tags are combined with AND. **Search** puts the filters in a validated hash route; document links retain them. Empty search results differ from an unindexed or empty catalog. Pagination remains bounded by the catalog API.
- Open a runbook to inspect its source, explicit version, applicability, review provenance and content/metadata digests. Current membership is checked independently of exact content, so a status/catalog error does not discard a historical read. The catalog has at most 100 documents, allowing one bounded unfiltered membership read.
- **Download exact document** exports the stored UTF-8 text with a versioned filename. **Download provenance** exports the document ID, version, source, applicability, review metadata, byte count and hashes as JSON. Browser save prompts and download policies still apply.
- Content is plain inert text. Markdown, HTML, links, images and scripts inside a document are never activated. An exact chunk link focuses the chunk heading and shows its stored heading path, byte range, ID and hash; the full document remains expandable.

On a selected persisted Run, both roles can read **Saved runbook guidance**. An Operator explicitly chooses **Find guidance**; the UI sends an empty request, waits for the server acknowledgement and shows the immutable saved selection. Failed actions require an explicit retry. A saved no-match is still a selection. Legacy Runs without captured scope need a new investigation. A new Run is also needed to refresh a saved manual selection.

Each citation uses the Run/selection-scoped resolver. Missing or mismatched references show an error without substituting current content. **Back to selected Run** retains the selected Run and safe investigation/queue context. Refresh and sign-in preserve only validated route fields; the existing session boundary clears protected query/mutation caches and never replays Find guidance.

**Knowledge index** is the same feature-owned component in Knowledge and Connections. It shows full-text mode, semantic-not-configured, active generation, last checked/success, retained history and degraded/updating limitations. **Refresh index status** only reads status. Corpus changes remain with the [trusted index CLI](runbook-index.md), and [Run guidance](run-guidance.md) remains methodology rather than telemetry, AI diagnosis or execution approval.

Focused browser evidence for this slice lives in [the acceptance receipt](../../evidence/knowledge61/acceptance.json). Full installed-stack release relevance remains with issue #62.
