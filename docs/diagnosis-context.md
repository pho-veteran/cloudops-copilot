# Scoped diagnosis context and evidence-bound findings

This document describes the fixed `e06.*/v1` context, evidence-reference and advice
validation rules implemented in `services/backend/src/cloudops/features/diagnosis`.
It is a methodology contract for issue #69, not a claim of diagnosis quality.

## Purpose and non-claims

The context builder selects a bounded, UID-scoped set of already retained evidence for
one persisted Investigation Run, labels every sent item with an `E###` alias, optionally
adds pinned runbook chunks as `K###`, and measures the exact request body before any
model call. Advice validation then proves that every citation resolves to an item that
was actually sent and still exists with the same identity.

The database-backed builder selects only the resolved workload UID and the supported
source catalog. Rows from another UID or catalog cannot enter messages or context pins.

Explicit non-claims:

- Structural validation is not semantic validation. It proves membership, scope, hash,
  version, coverage and candidate mapping only. `RCAResult/v1` always reports
  `quality="not_established"`; relevance and entailment belong to the frozen evaluation
  rubric owned by #72.
- This feature never approves or executes a recovery action. `candidate_template_id` is
  a label, not a restored plan or parameter set.
- Egress policy is an allowlist plus operator-supplied deny patterns. It is not a
  universal secret detector.
- `development_fixture` evidence is synthetic and can never make a production family
  eligible or become a supported cause.

## Source classes and families

Source-class allowlisting is deny-by-default (`EgressPolicy/v1`). A send is permitted
only when every used class is allowed.

| Slot | Source class | Required / optional cap | Order |
| --- | --- | --- | --- |
| `operator_request` (request envelope) | `operator_request` | not citable | — |
| `kubernetes.state` | `kubernetes_state` | 250 / 250 | key |
| `kubernetes.configmap.metadata` | `kubernetes_state` | 250 / 250 | key |
| `kubernetes.events` | `kubernetes_events` | 50 / 20 | newest |
| `kubernetes.logs.current` | `container_logs` | 3 / 1 per group | newest |
| `kubernetes.logs.previous` | `container_logs` | 3 / 1 per group | newest |
| `prometheus.request_rate`, `error_ratio`, `p95_latency`, `memory_working_set`, `memory_limit`, `restarts` | `prometheus_series` | 250 / 250 | key |
| `development_fixture` | `synthetic_fixture` | 0 / 20 | key |
| runbook chunk | `runbook` | 999 aliases total | retrieval |

The five assessment families and their required slots, in fixed order:

| Family | Required slots |
| --- | --- |
| `crashloop_startup` | state, events, logs.current, logs.previous |
| `oom_restarts` | state, events, logs.current, logs.previous |
| `traffic_latency_errors` | request_rate, error_ratio, p95_latency |
| `pending_unschedulable` | state, events |
| `image_pull` | state, events |

A family is eligible only when every required slot is usable. `not_applicable` log
sources are waived and recorded as `not_applicable`/`waived`; missing, unavailable,
expired, unsupported-projection or synthetic slots are not waived. Partial coverage
still allows `supported` but blocks `ruled_out_hypotheses`. Rule-based sampling,
ambiguous-entry removal and budget omissions in a required sent slot also block a
ruled-out conclusion, even when the original source reports complete availability.

`source_coverage` always starts with the envelope-only `run.request` row
(`source_class="operator_request"`, `status`/`reasons` reflecting capture, deny scan and
allowlist). It has no evidence id and is never a citable target. Optional material is
selected only from allowlisted, available slots; a policy-denied runbook selection is
omitted as `content_denied` with the `runbook_policy_denied` limitation, so a built
context is always sendable under the policy that built it.

## Versions and identities

| Identity | Value | Meaning |
| --- | --- | --- |
| Context | `e06.context/v1` | prepared-context shape |
| Target rules | `e06.targets/v1` | extraction caps and order |
| Prompt | `e06.rca_prompt/v2` | fixed system/user envelope |
| Validator | `e06.validator/v1` | structural + exact-reference checks |
| Probe prompt | `e06.probe_prompt/v1` | transport-only probe |
| Policy | `EgressPolicy/v1` | allowlist, destination, deny patterns |

`PreparedContext.digest` covers versions, `policy_digest`, scope, request source/text,
the sent manifest and the final messages. `PreparedContext.record()` is a persistence
summary: it omits request text and target values. `InsufficientContext` is the persistable
insufficiency record (`ContextInsufficiency/v1`): its digest covers versions,
`policy_digest`, reason, scope, source coverage, families, omissions and limitations, and
its `record()` keeps `request_source`/`request_present` without the request text.

Each sent target preserves the exact projected entry `identity` alongside its value
and locator. Metric samples therefore retain their series labels and timestamp rather
than becoming unassociated numbers. Identities undergo the same deny scan as values
and count toward the serialized whole-message budget. `value_bytes` counts the value;
the whole-message measurement also includes identities and envelope metadata.

`build` reloads the supplied Knowledge selection from its persisted analysis consumer
and matching `analysis_identity`; a manual selection or another analysis's selection
returns `analysis_selection_mismatch` without a send. Pure `assemble` rejects manual
views; callers needing persisted identity verification use `build`.

## Aliases, budget and repair

- Required targets are extracted before budgeting and are never byte-trimmed. Any
  single required target over `policy.max_target_bytes`, or an alias count over 999,
  fails the whole build; the family set is never silently shrunk.
- Optional targets are added greedily only while the whole request fits
  `effective_budget_bytes - 4096` (`REPAIR_RESERVE_BYTES`). The effective budget is
  `min(policy.max_message_bytes, profile.limits.max_input_bytes)`.
- `message_bytes` is always `len(messages.serialize_payload(profile, messages))`; it is
  the only byte measurement used for a send decision.
- `repair_available` is true when the final messages fit below the repair reserve. One
  repair appends at most 10 failures and 2048 bytes to the same policy-approved user
  content. Oversized or non-fitting repair is `repair_unavailable`; rejected model text
  is never re-sent, stored or logged.

Closed omission codes: `content_denied`, `target_too_large`, `alias_space_omitted`,
`budget_omitted`. Closed insufficiency reasons: `scope_unavailable`,
`request_not_captured`, `request_content_denied`, `policy_denies_required_class`,
`message_shape_invalid`, `request_exceeds_budget`, `no_evidence`, `no_eligible_family`,
`required_alias_space_exceeded`, `required_target_too_large`, `required_content_denied`,
`required_targets_exceed_budget`.

## Send phases and egress

`check_send` rebuilds the exact messages for `initial`, `retry` or `repair` and applies,
in order: policy presence, policy version/digest equality, profile identity equality,
destination equality, repair bounds, message shape, used-class allowlist, deny-pattern
scan (request text, target values/labels, runbook content/title, feedback, message
content) and serialized byte budget. Any failure returns a closed code and no bytes;
the only authorizing success is `SendCheck(ok=True)` with digest and byte count.

`prepare_probe` builds the fixed marker request. It reads no Run or evidence, requires
`diagnostic_fixture` to be allowed, scans both fixed messages against deny patterns,
and can never create findings or grant quality. Invalid UTF-8 deny patterns are rejected
when loading policy, before digesting or sending.

## Advice validation

`advice.parse` rejects non-object, non-JSON or excessively nested text as `json_invalid`
and model failures as `schema_invalid` with a bounded path only. All D2 fields are
required, including explicit empty lists, the schema version and a nullable candidate.
Paths contain only known schema field names and numeric indices; unknown property
names from rejected model output never enter repair feedback.
`advice.validate_structural` runs, in
order: alias membership, status rules, candidate mapping, ruled-out coverage and next
steps. `advice.validate` then resolves every cited alias read-only against the stored
evidence and the pinned Knowledge selection; any mismatch is terminal.

Closed validation codes: `json_invalid`, `schema_invalid`, `unknown_alias`,
`duplicate_alias`, `alias_wrong_class`, `runbook_only_support`,
`supported_requires_family_cause`, `supported_requires_family_evidence`,
`insufficient_requires_unknown_cause`, `candidate_not_allowed`, `candidate_mismatch`,
`ruled_out_family_unknown`, `ruled_out_family_ineligible`,
`ruled_out_evidence_outside_family`, `ruled_out_coverage_incomplete`,
`ruled_out_partial_coverage`, `empty_next_steps`, `reference_not_sent`,
`evidence_reference_mismatch`, `evidence_hash_mismatch`, `evidence_expired`,
`evidence_unavailable`, `unsupported_payload_version`, `runbook_alias_unknown`,
`runbook_reference_mismatch`.

Exact resolution verifies the Run/incident scope, source key, evidence SHA-256, payload
version, full projected entry digest, locator non-ambiguity, and every hashed field of
the pinned `KnowledgeRef` (including `selection_id`). A pinned old version stays
resolvable after reindexing; a current version is never substituted.

## Temporary pin lifecycle

`pins.acquire(db, analysis_id, context)` locks every distinct contributing Evidence row
(required and optional) in ascending `Evidence.id` order, re-reads availability under
the lock, and inserts one `EvidencePin(kind="analysis", owner_id=analysis_id)` per row.
If any contributor is expired, purged or unavailable it inserts nothing and returns
`PinDenied(reason="contributing_content_unavailable", slot, alias)`. It never
resurrects, updates or commits.

`pins.release(db, analysis_id)` deletes only that analysis's pins and returns the count.
`latest`, `resolution` and other owners are untouched, release is idempotent, and after
release an expired unpinned row is explicitly `expired` again rather than silently
successful. Purge skips any row with a pin, so a pin that wins the race keeps its
content.

## Evidence fixtures

`docs/evidence/e06-r2-context/` contains model-free fixtures generated from the real
builders: one prepared context, one supported and one insufficient `RCAAdvice/v1`, an
invalid-case matrix with expected validator codes, and an egress-denial matrix with
expected send/scan codes. `tests_unit` contract tests validate every fixture through the
same Pydantic models and the same validator/egress code paths; hashes are never
hand-edited.

`runtime-checks.json` records a separate owned PostgreSQL/loopback HTTP journey through
context building, initial/repair admission, exact citation validation and pin expiry.
It uses synthetic inputs and establishes no live model or cluster qualification.
