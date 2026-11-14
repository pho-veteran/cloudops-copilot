# Scoped Diagnosis Context and Evidence-Bound Findings — Design (E06 R2, issue #69)

**Date:** 2026-11-13
**Scope:** [R2 #69](https://github.com/pho-veteran/cloudops-copilot/issues/69) — bounded, policy-approved context and typed source-bound advice.
**Parent:** [EPIC E06 #9](https://github.com/pho-veteran/cloudops-copilot/issues/9), decisions **D1–D5** (authoritative for every limit and shape named here).
**Baseline:** `origin/main` @ `ea5a42c` (R1 #68 merged; E04/E05 accepted). R3 #70 is open and consumes this contract.
**Plan:** `docs/superpowers/plans/2026-11-13-diagnosis-scoped-context-plan.md`.
**Sibling contract:** `docs/superpowers/specs/2026-11-13-diagnosis-analysis-lifecycle-design.md` (#70) — §13 lists the four R2 contracts this document must provide.

---

## 1. Intent and why

Collection now stores exact, redacted, bounded Run evidence, and E05 pins exact runbook
versions. Neither can become diagnosis input yet. #69 turns those stored rows into one
**bounded, deterministic, policy-approved** message pair, and accepts only a typed
`RCAAdvice/v1` payload whose evidence and runbook aliases resolve to the exact refs that
were sent. Everything else is an explicit insufficiency or a typed rejection.

Success is a **model-independent contract with deterministic fixtures** that #70 can
orchestrate — initial send, retries, one repair, and a harmless probe — without #70
re-deciding source selection, aliases, budgeting, or validation.

### Anti-goals (what would make this a failure even if it "works")

- Inferring a diagnosed family from the question, pack coverage, retrieval hits, or model
  output. Family eligibility is a **permission to assess**, never a diagnosis.
- Treating E04 pack coverage (or a 1 KiB UI preview) as sent-context evidence.
- Trimming required targets, or shrinking the pre-budget family set, to fit the input budget.
- Sending when no family is eligible, when the required union does not fit, or when a
  required class is policy-denied.
- Turning a `development_fixture` Run into a production finding because a model is enabled.
- Repairing a refusal, replaying an unknown response, or echoing rejected model output,
  secrets, credentials, raw telemetry, provider bodies, or error fixtures into a message,
  a feedback string, or a log.
- Treating structural validity as truth, entailment, or diagnostic quality.
- Persisting context/messages, adding a migration, an HTTP operation, or any lifecycle,
  retry, quota, slot, or transport behavior — all of that is #70.

## 2. Goals and non-goals

**Goals**

1. `EgressPolicy/v1`, `PreparedContext/v1`, `RCAAdvice/v1` and normalized `RCAResult/v1`
   with versioned identities and Pydantic `extra="forbid"` on model-facing objects.
2. Server-resolved Run scope, the D2 five-family required-source matrix, separate source
   coverage and sent-context coverage, and a deterministic, versioned required-target union.
3. Deterministic whole-message assembly under the D1 ceiling, optional-material selection
   with documented omissions, and no-call insufficiency when mandatory content is missing,
   denied, or does not fit.
4. Allowlist-based final egress checks for **initial, retry, repair and probe** payloads.
5. Alias→exact-reference validation against the **sent** context, including hash, scope,
   version, time relation, coverage and candidate-template rules, normalized to
   `RCAResult/v1`, with bounded structural feedback for at most one repair.
6. Temporary analysis-owned evidence pins using existing `EvidencePin` mechanics, released
   on terminal outcomes, proven against purge races and other pin owners.

**Non-goals**

- Any HTTP operation, OpenAPI/TypeScript change, permission-inventory entry, or web fixture
  (D4 and #71 own exposed contracts).
- Any Alembic migration. `PreparedContext`/result persistence belongs to #70
  (`diagnosis_contexts`, `diagnosis_results`); #69 only returns immutable in-memory records
  plus a secret-free summary for #70 to store.
- Any provider call, retry/repair orchestration, call budget accounting, quota handling,
  deadline, slot guard, job/dispatch change, qualification gate (D5), evaluation driver,
  or UI. #69 never imports R1 `transport` or R3 lifecycle code.
- Real model quality, semantic relevance, entailment, or live-endpoint compatibility
  (D5 / #72), and any E07 plan, fresh eligibility, parameters, patch, or execution.
- New secret detection guarantees. Deny patterns are a bounded defence-in-depth layer, not
  universal secret detection.

## 3. Constraints

- Baseline `origin/main` @ `ea5a42c`; branch from it. Tests require PostgreSQL only where
  DB reads/pins are involved; test databases end in `_test`.
- `features/diagnosis/` owns all new policy; `shared/config.py` stays free of diagnosis
  policy (R1 precedent). Entrypoints/#70 load configuration files.
- No migration head is created; #70 keeps the single Alembic sequence. If #70's migration
  lands first, #69 rebases without touching it.
- Reuse E04 exactly: `evidence_views`/`evidence_content` (`features/investigations/storage.py`,
  `service.py`), `locators.project` (`v1`), `CitationRef`/`Locator` and
  `comparison_service.citation`. Never use `Entry.view()` (the 1 KiB preview) as context.
- Reuse E05 exactly: `KnowledgeRunScope`, `KnowledgeRetrieval`, `KnowledgeRef`,
  `pin_guidance(consumer="analysis", request_identity=…)`, `resolve_selection_chunk`.
  Never reuse the `manual` selection; never substitute a current version.
- Reuse R1 primitives only: `Message`, `ModelProfile.limits.max_input_bytes`,
  `CallIdentity`/`ModelCall`/`CallKind`. `features/diagnosis/messages.py` serialization is
  the byte-measurement reference.
- No new logging of payload text. Typed codes and counts only.
- English only in code, fixtures, docs and prompts. No product UI copy in this task.

## 4. Current state (source-inspected at `ea5a42c`)

| Surface | Current behaviour reused/gapped |
| --- | --- |
| `investigations.storage.store_batch/finalize_batch` | Per-source redacted/bounded evidence with `source_key`, `coverage`, `time_relation`, `sha256`, `expires_at`, `pack_coverage`. Pins `latest` per incident. |
| `investigations.storage.evidence_views` | `content_status` = `available`/`unavailable`/`expired`, honouring any `EvidencePin`; `pinned` flag. |
| `investigations.service.evidence_metadata/evidence_content` | Scoped exact reads; content returns `EvidenceContent` or `410 evidence_expired`/`409 evidence_unavailable`. |
| `investigations.locators.project(kind, version, payload)` | Deterministic `Entry(kind, identity, label, value, multiplicity, ambiguous)`; `.key` = `v1_<sha256>`; `version != 1` unsupported; `.view()` truncates to 1 KiB (preview only). |
| `investigations.comparison_service.citation` | Re-projects stored payload, returns `CitationResolution`; 404 missing, 409 ambiguous. |
| `investigations.catalog._PACKS` | The five families and their required/optional slots; E04's own availability derivation. |
| `knowledge.guidance` | `_scope` resolves Run→Incident→Workload identity; `pin_guidance` creates one immutable per-`(run, consumer, request_identity)` selection; `retrieve` statuses `matched/partial/no_matches/not_indexed/unavailable`; `resolve_selection_chunk` validates membership and hashes. |
| `diagnosis.messages`/`profile`/`schemas` | Exactly one system + one user message, control-character rejection, `stream=false`, one output-limit parameter, `max_input_bytes` (default 65536), `ModelCall`/`CallKind`. No context, egress or validation. |
| `RunView.diagnosis` | Hard-coded `"unavailable"`; no analysis API or persistence. |
| No `features/diagnosis/{context,egress,advice,pins}`, no evidence pin kind `analysis`, no egress policy file. |

### 4.1 Task 0 alignment (confirmed on branch `feat/diagnosis-69-scoped-context`)

#68 (R1) is merged at `ea5a42c`; the names assumed above are exact: `diagnosis/messages.py`
(`Message`, `MessageError`, `prepare_payload`), `diagnosis/profile.py` (`ModelProfile`,
`OutputLimitParam`), `diagnosis/schemas.py` (`ModelCall`, `CallKind`), `diagnosis/transport.py`.
The shared test fixture helper is `services/backend/tests/diagnosis_fixtures.py`
(`profile(**overrides)`, canonical envelopes), imported from `tests_unit/` as
`from diagnosis_fixtures import profile` because pytest sets `pythonpath = ["tests"]`.
Two additive helpers land before any new module: `messages.serialize_payload(profile, messages)`
owns sendable-profile validation, normalization, body construction and UTF-8 encoding, with
`prepare_payload` delegating and then enforcing `max_input_bytes`; and
`knowledge.guidance.run_scope(db, incident_id, run_id)` delegates to the existing
`_scope(db, run, None, None)` so #70 reads exactly the scope `pin_guidance` used.

## 5. Architecture

```
features/diagnosis/
├── versions.py     # context/target/prompt/validator identities (single source)
├── egress.py       # EgressPolicy/v1, loader, destination/class/pattern checks
├── context.py      # scope snapshot, source matrix, target rules, budget, assembly, record
├── advice.py       # RCAAdvice/v1 + RCAResult/v1 + parse/normalize/validate + feedback
├── pins.py         # temporary EvidencePin(kind="analysis") acquisition/release
└── (existing messages.py / profile.py / schemas.py / transport.py — only the additive `serialize_payload` in messages.py)
```

Composition boundary:

- **#70 calls** `knowledge.pin_guidance(consumer="analysis", request_identity=<stable analysis id>)`
  first (its transaction, its selection lifecycle), then
  `context.build(db, incident_id, run_id, selection=<RunGuidance.selection | None>,
  policy=..., profile=..., analysis_identity=...)`. A `RunGuidance.state="unavailable"`
  (index missing/degraded) passes `None` and becomes a runbook limitation, never a gate.
  The sendable `ModelProfile` is required because R1's serializer defines the byte
  measurement.
- **#70 calls** `pins.acquire(db, analysis_id, context)` immediately after a successful build
  and before any send; `pins.release(db, analysis_id)` on every terminal outcome.
  A `pin_denied` result is resolved by at most one **pre-send rebuild** (no HTTP call has
  happened), then terminalizes (D1 budgets HTTP calls, not context builds).
- **#70 calls** `egress.check_send(...)` immediately before **every** send (initial, retry,
  repair) and `advice.validate(...)` after every text response; on invalid text it may call
  `advice.feedback_message(...)` once (D1: at most one repair).
- **#70 calls** `egress.prepare_probe(...)` for `diagnosis_probe_v1`; no Run context is read.
- **#69 never** calls R1 `transport.send`, writes `diagnosis_*` tables, commits, opens a
  network socket, or changes `InvestigationRun`.

## 6. Schemas and identities

### 6.1 Version identities (`versions.py`)

| Constant | Value (v1) | Meaning |
| --- | --- | --- |
| `CONTEXT_VERSION` | `e06.context/v1` | Envelope/coverage record shape |
| `TARGET_RULES_VERSION` | `e06.targets/v1` | Extraction, caps, ordering, alias assignment |
| `PROMPT_VERSION` | `e06.rca_prompt/v1` | System instruction + user envelope format |
| `VALIDATOR_VERSION` | `e06.validator/v1` | Machine-check matrix and normalizer |
| `PROBE_PROMPT_VERSION` | `e06.probe_prompt/v1` | Harmless probe messages |
| `EGRESS_POLICY_VERSION` | file-supplied (`EgressPolicy/v1`) | Deployment policy identity |

All versions are written into `PreparedContext`, `RCAResult` and the #70 rows. Changing any
of them requires a new version string, not an in-place edit.

Two non-versioned bounds live in `versions.py` as module constants, are copied into every
context record, and are part of `e06.targets/v1`'s semantics: `ALIAS_CAPACITY = 999`
(three-digit `E`/`K` alias space, §8.2) and `REPAIR_RESERVE_BYTES = 4096` (optional
material leaves room for one bounded repair, §8.3/§10.1).

### 6.2 Source classes (D2)

`operator_request`, `kubernetes_state`, `kubernetes_events`, `container_logs`,
`prometheus_series`, `runbook`, `synthetic_fixture`, `diagnostic_fixture` (probe only).

Mapping from E04 `source_key`:

| `source_key` | class |
| --- | --- |
| `kubernetes.state`, `kubernetes.configmap.metadata` | `kubernetes_state` |
| `kubernetes.events` | `kubernetes_events` |
| `kubernetes.logs.current`, `kubernetes.logs.previous` | `container_logs` |
| `prometheus.*` | `prometheus_series` |
| `development_fixture` | `synthetic_fixture` |
| stored Run question | `operator_request` |
| E05 analysis selection chunks | `runbook` |
| probe fixture | `diagnostic_fixture` |

`development_fixture` **never** counts toward family eligibility and can never satisfy a
production required slot (D2, AC4). It may appear only as optional `synthetic_fixture`
material when the policy allows it, with the limitation
`synthetic_fixture_not_production_evidence`.

`operator_request` is the **stored Run request text**, not a citable evidence row:

- It is carried once as `PreparedContext.request = {text, source}` where `source` is the
  stored `question_source` (`operator`, `title_default`, `automatic`). It appears in the
  user envelope but is **never assigned an `E` alias**, never a `ContextTarget`, and never
  citable — a model can never "support" a cause with the operator's own wording.
- `question_source = "not_captured"` or a null/blank question is
  `request_not_captured` insufficiency, never a fabricated prompt (§7.1, §14.6).
- A non-`operator` source adds the limitation `request_not_operator_authored`; it changes
  no eligibility rule.
- The text is deny-pattern scanned like any other class: a match is
  `request_content_denied` (there is no optional fallback — the request is required).
- The `operator_request` class must be allowlisted, or the build is
  `policy_denies_required_class`. Its coverage row (`slot="run.request"`) is part of
  `source_coverage`, so §9.2's manifest check covers it.

### 6.3 `EgressPolicy/v1` (`egress.py`)

```json
{
  "schema": "EgressPolicy/v1",
  "policy_id": "example-deny-all",
  "policy_version": "1",
  "destination": { "config_id": "example-evaluation", "config_version": "1",
                   "endpoint_label": "example-invalid" },
  "allowed_source_classes": [],
  "max_message_bytes": 65536,
  "max_target_bytes": 8192,
  "redaction_policy_version": "redaction/v1",
  "deny_patterns": [
    { "name": "authorization_header", "regex": "(?i)\\bbearer\\s+[A-Za-z0-9._~+/-]{16,}" },
    { "name": "private_key_block", "regex": "-----BEGIN [A-Z ]*PRIVATE KEY-----" },
    { "name": "assigned_secret", "regex": "(?i)\\b(password|passwd|token|secret|api[_-]?key)\\s*[:=]\\s*\\S{8,}" }
  ]
}
```

Rules:

- `extra="forbid"`, frozen; `policy_id`/`policy_version`/`redaction_policy_version` are
  non-empty strings ≤64; `max_message_bytes` 1..65536; `max_target_bytes` 256..65536 and
  ≤ `max_message_bytes`; ≤20 deny patterns, each a compiled, size-bounded regex.
- **Deny by default:** `allowed_source_classes=[]` denies every class. Absent file = no
  policy = no prepared send. A class not listed is denied.
- `destination` binds the exact `config_id`/`config_version`/`endpoint_label` from R1's
  `CallIdentity`; a mismatch fails the send check. It never contains a URL, credential, or
  secret. The **full** `CallIdentity` (including `behavior_digest`, `requested_model`,
  `authorization_id`, `budget_policy_id`) is bound into `PreparedContext.profile_identity`
  and recompared on every send, so a profile edit invalidates a prepared context even when
  the three policy fields are unchanged.
- `digest()` = SHA-256 of canonical JSON of the policy (sorted keys) → `policy_digest`.
- `check_send(...)` compares the policy version/digest, destination and the context's bound
  `profile_identity` on **every** send; a policy or profile change between context and send
  invalidates the context (`profile_identity_changed` / `policy_changed`).
- The deny patterns are applied to the exact assembled message strings. A match makes the
  content **unusable**, never silently redacted: the offending target is denied (required ⇒
  insufficiency; optional ⇒ omission) and no matched text is stored, logged, or returned.
- `config/diagnosis-egress.example.json` ships the deny-all example; #70 entrypoints load
  the trusted path (e.g. `DIAGNOSIS_EGRESS_POLICY`), never a browser value.

### 6.4 `PreparedContext/v1` (`context.py`)

Fields (frozen Pydantic, `extra="forbid"`):

| Group | Fields |
| --- | --- |
| Identity | `schema_version="PreparedContext/v1"`, `context_version`, `target_rules_version`, `prompt_version`, `policy_version`, `policy_digest`, `validator_version`, `analysis_identity` (caller-supplied stable string `[A-Za-z0-9._:-]{1,128}`), `profile_identity: CallIdentity` |
| Scope | `run_id`, `incident_id`, `workload_id`, `workload_uid`, `cluster`, `namespace`, `name`, `kind="Deployment"`, `window {start,end,source}` |
| Request | `request {text, source}` — untrusted data, never aliasable (§6.2) |
| Knowledge | `selection_id: UUID \| null`, `runbook_status`, `runbook_limitations` (from `KnowledgeSelectionView.retrieval`; `None` selection ⇒ `selection_id=null`, `runbook_status="not_selected"`) |
| Coverage | `source_coverage: list[SourceCoverage]`, `sent_coverage: SentCoverage` |
| Matrix | `families: list[FamilyAssessment]` (all five, fixed order) |
| Manifest | `required_targets`, `optional_targets: list[ContextTarget]`, `runbooks: list[ContextRunbook]`, `omissions: list[Omission]`, `limitations: list[str]` |
| Budget | `effective_budget_bytes`, `repair_reserve_bytes`, `repair_available`, `alias_capacity` |
| Message | `messages: list[Message]` (exactly 1 system + 1 user), `message_bytes`, `content_bytes`, `digest` (SHA-256 of canonical `{versions, policy_digest, scope, request, manifest, messages}`) |

`ContextTarget` = `alias ("E001"…)`, `evidence_id`, `source_key`, `source_class`,
`locator {kind,key}`, `label` (≤120), `value` (full `Entry.value`, never `Entry.view()`),
`entry_digest`, `evidence_sha256`, `time_relation`, `coverage_status`, `value_bytes`,
`family_slots` (which eligible families required it).

`ContextRunbook` = `alias ("K001"…)`, `selection_id`, full `KnowledgeRef/v1`
(`document_key`, `document_version_id`, `version`, `chunk_id`, `chunker_version`,
`document_sha256`, `metadata_sha256`, `chunk_sha256`), `title`, `heading_path`,
`content` (full pinned chunk text), `content_bytes`, `truncated`.

`record()` returns a secret-free summary for #70 persistence: everything above except
`messages`, the request **text** (it keeps `request_source` and `request_present`) and
target `value` text. `InsufficientContext` exposes the same `record()` shape (its
`outcome="insufficient"`, `message_bytes=None`), plus the same `versions`, `scope`
(nullable), `request_present: bool` (never the text), `digest` and `policy_digest`, so #70
writes one `diagnosis_contexts` row for either outcome and never needs a second schema. #70
stores that summary, the digest and byte counts. Rebuilding from the same
evidence/policy/selection must reproduce the same digest (tested).

### 6.5 `RCAAdvice/v1` (`advice.py`) — exactly D2

Pydantic with `extra="forbid"`, list `max_length=20`, unique aliases within each list,
general strings non-empty/max 2000 unless narrower:

| Field | Rule |
| --- | --- |
| `schema_version` | `Literal["RCAAdvice/v1"]` |
| `status` | `supported \| insufficient_evidence` |
| `cause_category` | one of the five family IDs or `unknown` |
| `summary` | 1..4000 |
| `supporting_evidence_ids`, `contradicting_evidence_ids` | telemetry citations `^[EK][0-9]{3}$`; a `K` alias is rejected as `alias_wrong_class` (§10), never accepted as telemetry |
| `alternatives` | `{hypothesis: 1..200, reason: 1..2000, evidence_ids: list}`; a reason may cite none |
| `ruled_out_hypotheses` | `{family: family-ID, reason: 1..2000, evidence_ids: nonempty list}` |
| `unverified_checks` | `{check: 1..200, reason: 1..2000}` — no citation field |
| `missing_evidence`, `limitations`, `next_steps` | string lists; `next_steps` ≥1 |
| `runbook_citation_ids` | runbook citations `^[EK][0-9]{3}$`; an `E` alias is rejected as `alias_wrong_class` (§10), never accepted as a runbook |
| `candidate_template_id` | `restore_deployment_template \| set_memory_profile \| scale_deployment \| null` |

Citation fields deliberately share the `^[EK][0-9]{3}$` shape so that a well-formed
wrong-class alias reaches `validate_structural` as the repairable `alias_wrong_class`
code instead of becoming an indistinct `schema_invalid` parse error. Class enforcement
therefore lives in the structural validator, not in the model regex.

### 6.6 `RCAResult/v1` (`advice.py`) — normalized output

```
schema_version, run_id, incident_id, context_digest, validator_version,
prompt_version, context_version, policy_version, status, cause_category,
summary, supporting[], contradicting[], alternatives[], ruled_out[],
unverified_checks[], missing_evidence[], limitations[], next_steps[],
runbook_citations[], candidate_template_id, machine_validation="structural_v1",
quality="not_established"
```

Resolved evidence reference = `{alias, citation: CitationRef, source_key, source_class,
evidence_sha256, entry_digest, time_relation, coverage_status}`. Resolved runbook =
`{alias, selection_id, reference: KnowledgeRef}`. `quality` is a literal non-claim: #69
never establishes semantic relevance or diagnostic quality (D2/D5; rubric is #72).

### 6.7 Insufficiency and validation outcomes

`ContextBuildResult = PreparedContext | InsufficientContext`. `InsufficientContext` carries
`schema_version="ContextInsufficiency/v1"`, `reason` (closed enum), the full
`source_coverage`, `families` matrix, `omissions`, `limitations`, `versions`,
`policy_digest`, `scope` (nullable when `scope_unavailable`), `digest` (SHA-256 of canonical
`{versions, policy_digest, reason, scope, coverage, families, omissions, limitations}`) and
**no messages**. Reasons:
`request_not_captured`, `request_content_denied`, `request_exceeds_budget`,
`scope_unavailable`, `no_evidence`, `no_eligible_family`,
`required_targets_exceed_budget`, `required_target_too_large`,
`required_projection_unsupported`, `required_content_denied`,
`required_alias_space_exceeded`, `policy_denies_required_class`, `policy_invalid`.

`pin_denied` is **not** a build reason: pins are acquired after a successful build (§11),
so a pin denial is a `PinResult` that #70 handles without rewriting the context.

`ValidationOutcome` = `status: valid|invalid`, `result: RCAResult | null`,
`failures: list[ValidationFailure]`, `repairable: bool`.
`ValidationFailure = {code, path, repairable}` with a closed code enum (§10.3), a JSON path
such as `supporting_evidence_ids[2]` (≤120 chars), and **no value, text, or alias content**
beyond the path.

## 7. Scope and the eligibility matrix

### 7.1 Server scope

`context.resolve_scope(db, incident_id, run_id)` returns the same `KnowledgeRunScope` E05
uses (incident/run/workload identity, cluster/namespace/name/UID, window, `kind`), and
`None` when the Run is legacy (missing question/`not_captured`, missing window) or the
workload identity is incomplete. To avoid duplicated drift, E05 gains one additive public
helper (`knowledge.guidance.run_scope(...)`) that `_scope` already implements; #69 calls it.
No HTTP field, workload UID, family, template, policy or budget is ever accepted from a
caller other than trusted server code.

The stored request (`question` + `question_source`) is the `operator_request` class and is
required for a v1 context; missing it (`question_source="not_captured"`, null or blank) is
`request_not_captured` insufficiency (no fabricated prompt). A non-`operator` source keeps
the limitation `request_not_operator_authored` (§6.2). A missing, degraded or `no_matches`
runbook index is only a limitation — it never blocks an evidence-supported context and
never substitutes simulation guidance.

### 7.2 Source usability (per slot)

For every catalog slot present on the Run, evaluate:

| Condition | Usable? | Recorded |
| --- | --- | --- |
| evidence row absent | no | `missing` |
| `coverage.status = unavailable` | no | `unavailable` + reason codes |
| `coverage.status = not_applicable` | log slots: **waived**; others: no | `not_applicable` |
| `coverage.status = partial` | yes | `partial` (blocks only `ruled_out`) |
| `content_status = expired` | no | `expired` |
| `content_status = unavailable` | no | `unavailable` |
| payload kind/version unsupported by `locators.project` | no | `projection_unsupported` |
| class denied by policy | no | `policy_denied` |
| every selectable entry depends on a value matching a deny pattern | no | `content_denied` |
| `time_relation = extends_after_window` / `not_captured` | yes, with limitation | explicit, never an invented interval |
| ambiguous locator entries | entries omitted, slot still usable if ≥1 target remains | `ambiguous_omitted` count |
| `run.request`: question captured | yes | `operator_request` row; text is envelope-only |
| `run.request`: question missing/blank | no | `request_not_captured` |
| `run.request`: value matches a deny pattern | no | `request_content_denied` |
| `run.request`: class not allowlisted | no | `policy_denied` |

### 7.3 Five-family required-source table (D2, verbatim)

| Family | Required slots for sent-context eligibility |
| --- | --- |
| `crashloop_startup` | `kubernetes.state`, `kubernetes.events`, `kubernetes.logs.current`, `kubernetes.logs.previous` |
| `oom_restarts` | the same four |
| `traffic_latency_errors` | `prometheus.request_rate`, `prometheus.error_ratio`, `prometheus.p95_latency` |
| `pending_unschedulable` | `kubernetes.state`, `kubernetes.events` |
| `image_pull` | `kubernetes.state`, `kubernetes.events` |

A required log slot is waived **only** by its explicit `not_applicable` outcome (E04 rule).
Denied, not-configured, empty-by-error, expired or unsupported is never a waiver.
`development_fixture` satisfies none of these slots.

`FamilyAssessment = {family, eligible_for_assessment: bool, reasons: list[enum],
required_slots: list[{slot, status, usable, waived, targets}], partial: bool}`. This is
permission to assess, not a diagnosis; the context never contains a selected family.

**Eligibility algorithm (pre-budget, fixed order):**
1. Compute slot usability from §7.2, including policy classes.
2. A family is `source_policy_eligible` iff every required slot is usable or log-waived,
   the required classes are allowed, and its rule-based required target set is non-empty.
3. Build the **required-target union** over `source_policy_eligible` families, in the fixed
   family order, deduplicated by `(evidence_id, locator.kind, locator.key)`.
4. If no family is eligible, or the union alone (plus the request and envelope) exceeds
   the budget, or the union would need more than `ALIAS_CAPACITY` aliases, return
   `InsufficientContext`; **the family set is not shrunk and required targets are not
   trimmed**. Otherwise those families become `eligible_for_assessment` and optional
   material is selected. The repair reserve applies only to optional material (§8.3).
5. Missing traffic metrics therefore leave an adequately covered Kubernetes family
   eligible; an attempted required-log trim fails the whole build instead of producing a
   smaller family set.

## 8. Required targets, ordering and budget (`e06.targets/v1`)

### 8.1 Extraction

For each usable slot, `locators.project(kind, payload_version, payload)` yields entries.
Ambiguous entries are excluded from selection (recorded, never cited). Extraction rules
are **independent of the byte budget** and fixed by this version:

| Slot | Required rule | Optional rule |
| --- | --- | --- |
| `kubernetes.state`, `kubernetes.configmap.metadata` | all non-ambiguous entries, cap 250, ordered by `entry.key` | same |
| `kubernetes.events` | non-ambiguous entries, cap 50, newest first by event timestamp (`lastTimestamp`/`eventTime`/`creationTimestamp`, fallback `entry.key`) | cap 20 |
| `kubernetes.logs.current`, `kubernetes.logs.previous` | per `(pod_uid, container, previous)` group: 3 newest segments by identity timestamp (fallback `entry.key`) | 1 newest per group |
| `prometheus.*` | all non-ambiguous entries (series + samples), cap 250, ordered by `entry.key` | same |
| `development_fixture` | never required | cap 20 |

Each target keeps the **full** `Entry.value`; no `Entry.view()` preview and no byte
truncation is ever used. Rule-level count caps are recorded as `rule_omitted` completeness,
distinct from budget omissions. A single required target whose canonical value exceeds
`max_target_bytes` fails the build (`required_target_too_large`); an oversized optional
target is omitted with `target_too_large`.

**Alias space (pre-budget).** `RCAAdvice/v1` fixes three-digit telemetry aliases
(`E001`…) and three-digit runbook aliases (`K001`…) — 999 of each. Extraction is
therefore also bounded by `ALIAS_CAPACITY = 999` **required** targets: if the required
union needs a 1000th alias the build fails with
`InsufficientContext(required_alias_space_exceeded)` rather than widening the advice
contract or trimming a required target. This bound is a safety net, not an expected
budget: every target carries alias, id, source key, 67-char locator key, two 64-hex
digests and a label, so a 64 KiB message cannot hold anywhere near 999 targets. Optional
targets stop at the same cap and record `alias_space_omitted`.

### 8.2 Alias assignment and ordering

- Required targets: families in the D2 table order, slots in the table order above,
  targets in rule order → `E001`, `E002`, … (first occurrence wins on dedupe).
- Optional targets: remaining usable slots in catalog order (`kubernetes.configmap.metadata`,
  `prometheus.*` catalog order, non-required logs/events/state, `development_fixture`),
  rule order within each → continuation aliases, stopping at `ALIAS_CAPACITY`.
- Runbooks: selection order (E05 ranking order) → `K001`, `K002`, …; only
  `kind="runbook"`, `applicability="unverified"` is honest for v1.
- A rebuild with identical inputs, versions and policy must produce byte-identical
  messages and the same digest.

### 8.3 Budget and assembly

- Effective budget = `min(policy.max_message_bytes, profile.limits.max_input_bytes)`
  (D1 ceiling 64 KiB). `max_message_bytes` may be lower; it may never exceed 65536.
- **Byte measurement is R1's payload, not the two message strings.** `messages.prepare_payload`
  serializes `{model, messages, stream:false, <output-limit param>}` and enforces
  `max_input_bytes`. #69 adds one additive public helper, `messages.serialize_payload(profile,
  messages) -> bytes`, and `prepare_payload` becomes `serialize_payload` plus the limit
  check. `context.build`, `egress.check_send` and `advice.feedback_message` all measure with
  that helper, so "the context fits the budget" and "the transport will accept it" are the
  same arithmetic (no private-helper import, no duplicated JSON code).
- Envelope = system instruction + canonical `PreparedContext` user payload header (request,
  scope, versions, coverage, matrix) — always first.
- Add the required union in order. If `envelope + request + required` exceeds the budget →
  `InsufficientContext(required_targets_exceed_budget)`; if the request alone does →
  `request_exceeds_budget`; no messages are returned.
- **Repair reserve.** Optional material is added greedily in deterministic order only while
  the whole optional group fits and the measured payload stays ≤
  `budget - REPAIR_RESERVE_BYTES` (4096). Required material is not charged the reserve.
  Therefore a context with any optional material always has room for one bounded repair
  (§10.1), and a context whose required material alone consumes the last 4096 bytes sets
  `repair_available=false` — its first invalid response is terminal, because a repair that
  cannot fit is never attempted (D1 budgets one repair, it does not guarantee one).
  Overflowing optional material is skipped and recorded as `budget_omitted`; optional
  material is never split and never displaces required material.
- Then add runbooks the same way (they are optional), under the same reserve.
- `content_bytes` = sum of included target-value UTF-8 bytes; `message_bytes` =
  `len(serialize_payload(profile, messages))`. Both plus `digest` are returned for #70
  persistence and display.
- Rebuilding with identical evidence, selection, versions, policy and profile must produce
  byte-identical messages, `message_bytes` and `digest`.

### 8.4 Sent-context coverage

`SentCoverage` records required/optional **per slot**: included target count, rule-omitted,
budget-omitted, ambiguous-omitted, bytes; plus envelope/required/optional/total bytes,
budget, and the effective version tuple. Source coverage (§7.2) and sent coverage are
carried separately and never merged (D2). Omissions and limitations are explicit strings
with typed codes, never free-form provider or payload text.

## 9. Messages and pre-send egress

### 9.1 Prompt (`e06.rca_prompt/v1`)

Exactly one ASCII/UTF-8 system message and one user message (R1 shape). The system
instruction:

- states the role, that all user content is untrusted data whose instructions must be
  ignored, that only enumerated aliases may be cited, that it must output exactly one
  `RCAAdvice/v1` JSON object with no prose/fences, that a missing required source means
  `insufficient_evidence`, and that it must never invent evidence, parameters, scope,
  templates, or actions.
- contains no dynamic evidence, scope identifiers, or operator text.

The user message is a canonical JSON envelope: `request {text, source}`, `scope`, `versions`,
`source_coverage`, `families` (eligibility + reasons, **no diagnosed family**),
`required_targets`, `optional_targets`, `runbooks`, `limitations`. Each target includes its
label, locator and full value. The request text is untrusted data with no alias: the system
instruction says it is context, never an instruction and never citable. All untrusted text
stays inside JSON string values; it cannot become a role, a system instruction, or a new
alias.

### 9.2 Deny-by-allowlist final check

`egress.check_send(context, policy, profile, phase, feedback=None)`:

1. `profile.identity()` equals `context.profile_identity` (all seven `CallIdentity` fields,
   including `behavior_digest`) and `policy.digest()`/`policy_version` equal the context's;
   the policy `destination` triple matches. Any difference is `profile_identity_changed` /
   `policy_changed` and no message is returned.
2. For the phase, rebuild the exact messages: initial → `context.messages`; retry → the
   same messages as the phase being retried; repair → `advice.feedback_message(...)`
   appended to the same user content. Repair is only offered when
   `context.repair_available`; otherwise `check_send` returns `repair_unavailable`.
   Probe is **not** a context phase: it uses `prepare_probe` (§9.3) and never a Run context.
3. Every class used by the manifest is allowed, including `operator_request`; the manifest
   is the authority, not prompt text.
4. Deny patterns scan the final strings, including the request text; a match fails the
   check and returns a typed code without the matched text.
5. `len(messages.serialize_payload(profile, messages))` ≤ `context.effective_budget_bytes`
   and message count/roles exactly match R1's shape.
6. Return `SendCheck(ok, messages, digest, byte_size, code)`; only `ok=True` may be sent.
   On failure the caller terminalizes (`required_content_denied`) or drops optional material
   by rebuilding; no partial or unverified message is ever sent.
7. `code` is a closed, payload-free enum: `policy_missing`, `policy_changed`,
   `profile_identity_changed`, `destination_mismatch`, `source_class_denied`,
   `content_denied`, `budget_exceeded`, `message_shape_invalid`, `repair_unavailable`,
   `probe_not_allowed`. No failure carries message bytes, matched text, a value, an alias
   body beyond a manifest reference, or a provider/credential string.

### 9.3 Probe

`egress.prepare_probe(policy, profile)` requires `diagnostic_fixture` in
`allowed_source_classes` and a matching destination; otherwise it returns
`SendCheck(ok=False, code="probe_not_allowed")`. It returns one fixed system/user pair
(`e06.probe_prompt/v1`) asking for the exact marker `OK`, plus `expected_text="OK"`, digest
and byte count. It reads no Run, evidence, question, or secret, and can never create
findings. #70 judges transport/schema/quality distinctly; a probe never grants quality.

## 10. Advice validation (`e06.validator/v1`)

`advice.validate(db, context, raw_text)` is read-only and never sends. Order:

1. **Parse** strict JSON (`json.loads`, object only), then Pydantic `RCAAdvice/v1`
   (`extra="forbid"`). Failure → `json_invalid`/`schema_invalid` (repairable; bounded path).
2. **Alias membership** — every telemetry alias in supporting, contradicting, alternatives
   and ruled-out must be in the sent target manifest; every runbook alias in the sent
   runbook manifest. Wrong-class alias (`K` in a telemetry field / `E` in runbook field),
   unknown alias, duplicate-within-list → repairable codes. An unknown supporting alias is
   already reported here, so step 3 withholds the family-evidence conclusion until the
   citation set is valid.
3. **Status rules**
   - `supported`: `cause_category` is one of the current `eligible_for_assessment`
     families (never `unknown`); `supporting_evidence_ids` non-empty; at least one
     supporting alias's slot belongs to that family's required slots. Runbook citations
     never substitute for telemetry: a supported finding whose only support is
     `runbook_citation_ids` is `runbook_only_support`, and empty support with no runbook
     citation is `supported_requires_family_evidence`.
   - `insufficient_evidence`: `cause_category="unknown"`, `candidate_template_id=null`;
     supporting may be empty or name limited observations without asserting a cause.
4. **Candidate mapping** — `crashloop_startup → restore_deployment_template`,
   `oom_restarts → set_memory_profile`, `traffic_latency_errors → scale_deployment`, each
   also allowing `null`; any candidate on `pending_unschedulable`, `image_pull`, `unknown`
   or an insufficient result is invalid; a mismatched candidate is invalid.
5. **Ruled-out gate** — family must be eligible; `evidence_ids` non-empty; every cited
   alias belongs to that family's required slots; every **non-waived** required slot is
   represented by at least one cited alias (`ruled_out_coverage_incomplete`); every
   non-waived required slot's coverage is `available`, never `partial`, expired or ambiguous
   (`ruled_out_partial_coverage`). A slot waived by `not_applicable` (§7.3) demands no
   citation and no coverage, and adds the limitation `ruled_out_slot_waived:<slot>`; without
   this exclusion a crash/OOM family could never rule anything out when
   `kubernetes.logs.previous` does not apply. This is the machine gate; natural-language
   relevance stays a rubric judgment.
6. **Exact resolution** — for each alias, map to the sent `ContextTarget` and call
   `comparison_service.citation(db, incident_id, run_id, evidence_id, locator.kind, key)`.
   Verified: run/incident scope, locator membership and non-ambiguity, `evidence_sha256`
   equality, `source_key` equality, supported `payload_version`. For runbooks call
   `knowledge.resolve_selection_chunk(db, incident_id, run_id, selection_id, chunk_id,
   document_version_id)` and compare every hashed field. Any mismatch is terminal
   (not repairable): `evidence_reference_mismatch`, `evidence_hash_mismatch`,
   `runbook_reference_mismatch`, `evidence_expired`, `reference_not_sent`.
7. **Temporal honesty** — the sent target's `time_relation` and coverage are copied into
   the result; `not_captured`/`extends_after_window` remain explicit limitations. No closed
   historical interval is invented. Expired/unavailable reference content fails validation
   (temporary pins keep active context available until terminal).
8. **Normalize** to `RCAResult/v1` in the advice's order, with resolved refs and version
   identities; `quality="not_established"`, `machine_validation="structural_v1"`.

### 10.1 Repair feedback (at most one; #70 budgets it)

`advice.feedback_message(profile, context, failures)` appends a bounded block to the **same**
policy-approved user content:

```
VALIDATION_ERRORS (structural only):
- supporting_evidence_ids[0]: unknown_alias
- ruled_out_hypotheses[1]: ruled_out_partial_coverage
Return one corrected RCAAdvice/v1 JSON object only.
```

Only repairable codes and JSON paths are included; at most 10 failures and 2048 bytes. The
function returns `None` when appending the block would make
`messages.serialize_payload(profile, repaired)` exceed `effective_budget_bytes`; #70 then
treats the validation failure as terminal (`repair_unavailable`) instead of sending an
oversized request. The raw rejected response, its text, values, provider bodies, credentials
and error fixtures are never re-sent, stored or logged. A refusal/non-stop/truncated
`ModelCall` kind is never repairable and never becomes feedback (D1/D2); #70 classifies
those from `CallKind`.

### 10.2 Machine vs semantic

Machine checks cover structure, membership, scope, hash, version, time relation, coverage
and candidate mapping. They do **not** establish relevance, entailment, correctness or
quality. Acceptance for semantic relevance is #72's frozen rubric; #69's docs and result
say so explicitly (AC5, D2).

### 10.3 Validation code enum

`json_invalid`, `schema_invalid`, `unknown_alias`, `duplicate_alias`, `alias_wrong_class`,
`runbook_only_support`, `supported_requires_family_cause`,
`supported_requires_family_evidence`, `insufficient_requires_unknown_cause`,
`candidate_not_allowed`, `candidate_mismatch`, `ruled_out_family_unknown`,
`ruled_out_family_ineligible`, `ruled_out_evidence_outside_family`,
`ruled_out_coverage_incomplete`, `ruled_out_partial_coverage`, `empty_next_steps`,
`reference_not_sent`, `evidence_reference_mismatch`, `evidence_hash_mismatch`,
`evidence_expired`, `evidence_unavailable`, `unsupported_payload_version`,
`runbook_alias_unknown`, `runbook_reference_mismatch`.

## 11. Temporary analysis pins (`pins.py`)

`acquire(db, analysis_id, context) -> PinResult` is **atomic**:

- It collects the distinct contributing evidence ids of every included required **and
  optional** target, locks those `Evidence` rows by ascending `Evidence.id` (`FOR UPDATE`,
  the same order as `retention.purge_expired`), and re-reads `content_status` under the
  lock. It never resurrects expired content and never updates `Evidence`.
- If **any** contributing row is no longer `available` (expired, purged, unavailable), it
  inserts nothing and returns `PinDenied(reason="contributing_content_unavailable",
  slot, alias)`. A partially pinned context would send bytes whose source can no longer be
  verified, so the build is not silently rewritten. #70 may perform **one pre-send rebuild**
  (context building is read-only and not an HTTP call) and re-acquire; a second denial
  terminalizes as insufficient without a call.
- Otherwise it inserts one `EvidencePin(evidence_id, kind="analysis", owner_id=analysis_id)`
  per contributing row, idempotently (`ON CONFLICT DO NOTHING` semantics via existence
  check + flush, or `INSERT ... ON CONFLICT`). `owner_id` is the #70 analysis id; the
  `EvidencePin` table and purge mechanics are unchanged (no migration, no new kind
  constraint).
- `release(db, analysis_id)` deletes only `kind="analysis"` rows for that owner; `latest`
  (incident) and `resolution` owners are untouched. Release is idempotent and safe when the
  analysis failed before any pin was written.
- Purge race: purge locks rows first and sets `payload=NULL` only when no pin exists; a pin
  that wins makes purge skip. Tests cover both orders and assert no resurrection, no
  deletion of pinned content, and preservation of other owners after terminal release.

## 12. Acceptance-test traceability (issue #69 AC1–AC5)

| AC | Design sections | Planned proof |
| --- | --- | --- |
| **1** Persisted Run with unknown family, current/partial/missing/expired/ambiguous/outside-window sources, independently changed manual selection → correct server scope, exact analysis selection, explicit coverage/limitations; no family inference or current-version fallback | §7, §8, §6.4 | PostgreSQL `test_diagnosis_context_selection.py`: Run+workload+evidence matrix, manual `no_matches`/matched selection changed after, analysis selection by `request_identity`; assert scope/UID/window, selection independence, `source_coverage` vs `sent_coverage`, limitations per source, no `cause_category` anywhere in context, pinned old KnowledgeRef resolves after reindex |
| **2** Optional material over budget → deterministic documented omission; required union alone over budget or no eligible family → insufficiency, no prepared send; absent traffic metrics may leave a Kubernetes family eligible; trimming a required log target fails the whole build instead of shrinking the pre-budget family set; full locator values, never preview truncation presented as complete | §7.3, §8 | Pure `test_diagnosis_context_build.py`: byte-for-byte repeatability, greedy optional omission under the repair reserve, `repair_available=false` when required fills the budget, required-union overflow, required-alias-space overflow at 1000, no-family case, traffic-missing/Kubernetes-eligible case, required-log-overflow case with unchanged family matrix, 2 KiB locator value proves full value (no `…` preview), oversized required target fails, `message_bytes == len(serialize_payload(profile, messages))` and `prepare_payload` accepts it |
| **3** Source class/secret/identifier fixtures in initial, retry, repair and probe content → every permitted message obeys policy; rejected bytes do not leak through feedback/error strings; injection cannot change scope/model/config/template or enable parameters | §6.3, §9, §10.1 | `test_diagnosis_egress.py` + `test_diagnosis_advice.py`: deny-all, class allowlist (including `operator_request`), destination/`behavior_digest` mismatch, deny-pattern fixtures (bearer, key block, `password=`) in evidence **and in the request text**, messages contain no matched bytes and failures contain no payload text; retry/repair/probe each re-checked; repair refused when it would overflow the budget; injection text stays inside a JSON value, extra fields (`scope`, `model`, `template_id`, `parameters`) are rejected by `extra="forbid"` |
| **4** Valid supported/insufficient normalize exact refs; fabricated/unsent/cross-Run aliases, swapped hashes/chunks, unsupported versions, incomplete ruled-out coverage, runbook-only support, forbidden candidates rejected; partial observation never becomes ruled-out; `development_fixture` cannot become production diagnosis; evaluation remains separate | §6.5, §10, §7.3 | `test_diagnosis_advice.py` + DB `test_diagnosis_validation.py`: supported/insufficient golden fixtures, each invalid case returns its exact code, partial coverage blocks ruled-out, waived log slot does **not** block ruled-out and adds its limitation, the request text has no alias and can never be cited as support, fixture-only Run is insufficient and never eligible, evidence from another Run has no alias and is rejected, swapped `chunk_id`/hash rejected, template candidate matrix |
| **5** Manual no-match then analysis retrieval → independent selection and resolvable refs; source update/removal/purge race cannot delete active pinned context or historical Knowledge; terminal cleanup preserves other pin owners; later telemetry expiry explicit, not resurrection; structural validation distinct from semantic relevance/quality | §11, §10.2 | `test_diagnosis_pins.py`: manual no-match + analysis matched selection; purge/pin both orders; pinned content survives update/removal; a race that expires an **optional** contributing row denies atomically (no partial pins) and allows one pre-send rebuild; release preserves `latest`/`resolution`; after release+expiry `content_status="expired"` (no resurrection); `RCAResult.quality="not_established"` and docs state structural ≠ semantic |

## 13. Expected output and evidence

- Feature module `features/diagnosis/{versions,egress,context,advice,pins}.py`, no migration,
  no HTTP operation, no permission-inventory change, no contract regeneration (nothing new is
  exposed; #70/#71 own exposition).
- Deterministic JSON fixtures under `docs/evidence/e06-r2-context/`: one prepared-context
  summary (valid), one supported and one insufficient `RCAAdvice`, one invalid-case matrix
  with expected codes, and one egress-denial matrix. A focused test validates each fixture
  against the Pydantic schemas (producer/consumer review without a model).
- `docs/diagnosis-context.md`: family matrix, versions, budget/omission semantics, alias and
  repair rules, pin lifecycle, and explicit non-claims (methodology only; no diagnosis
  quality; no execution authority; no universal secret detection).
- `config/diagnosis-egress.example.json` (deny-all) and `.env.example` comment for the
  trusted policy path, following R1's profile precedent.
- Evidence record: commit/environment, focused pure/DB test output, backend lint/format/
  backend suites, and the fixture matrix. Controlled model responses (owned loopback)
  prove validation only; they do not prove inference quality or authorized egress (#72).

## 14. Builder calls (settled; override only with a parent decision)

1. **No migration and no persistence in #69.** `PreparedContext.record()` is a summary; #70
   owns `diagnosis_contexts`/`diagnosis_results` and the single Alembic sequence. This keeps
   the D2 coordination clause (no competing head) true by construction.
2. **`#69` reads, `#70` writes.** Context build is read-only; the analysis selection is
   created/reused by #70 via E05 `pin_guidance` and passed in. Pins are a separate explicit
   helper so #70 controls the transaction boundary.
3. **Effective budget = `min(policy.max_message_bytes, profile.limits.max_input_bytes)`**,
   defaulting to 65536 per D1, measured on R1's whole request body (`model`, `messages`,
   `stream`, output-limit parameter), not on the concatenated message text.
4. **Target extraction caps/order (`e06.targets/v1`)** as in §8.1. Rule-level caps are
   pre-budget and recorded; byte-budget trimming of required targets is impossible by design.
   A single oversized required target fails the build rather than being truncated or
   previewed.
5. **Reduce to `record()` for persistence, keep `messages` in memory.** Rebuild determinism
   is the takeover guarantee; #70 never stores message text in the context row.
6. **Question required in v1.** A legacy Run without a captured question yields
   `request_not_captured` without a call rather than a fabricated prompt.
7. **Runbook retrieval is a limitation, not a gate.** `not_indexed`/`unavailable`/
   `no_matches` never blocks evidence-supported context; family applicability stays
   `unverified`; no template expansion in v1.
8. **Partial coverage blocks `ruled_out`, not `supported`.** This implements D2's
   "partial observation stays unverified" and AC4's "never becomes a ruled-out hypothesis
   automatically".
9. **Repair is the same context plus bounded structural codes** appended to the user
   message (R1 allows at most 2 messages), never the raw rejected output. A repair that
   cannot fit the budget is not attempted (`feedback_message` returns `None`).
10. **Deny patterns deny, never redact.** Silent redaction would fabricate evidence; a deny
    makes required content an insufficiency and optional content an omission.
11. **`development_fixture` is never eligibility evidence**, even when a model is enabled;
    labelled evaluation cohorts use proper source keys or R5's separate driver.
12. **Additive E05 helper** `knowledge.guidance.run_scope(...)` to avoid duplicated scope
    logic; it changes no existing E05 behaviour.
13. **The request is envelope data, never an alias.** It is required (`request_not_captured`),
    deny-scanned, policy-gated, recorded with `question_source`, and un-citable; classifying
    it as a target would let the model confirm a cause with the operator's own words.
14. **Alias capacity is 999 required and 999 optional**, matching `RCAAdvice/v1`'s
    three-digit contract. A 1000th required target fails the build; a 1000th optional target
    is omitted. The byte budget binds far earlier (minimum target JSON is >250 bytes), so
    this is a correctness bound, not a practical truncation.
15. **Byte measurement is one shared R1 serializer.** Add
    `messages.serialize_payload(profile, messages)`; `prepare_payload` delegates to it and
    keeps its `max_input_bytes` check. `context.build` takes the sendable `ModelProfile`,
    so the budget it enforces is exactly what the transport will accept.
16. **`PreparedContext` binds the full `CallIdentity` + `behavior_digest` + `policy_digest`,
    not just the destination triple.** `check_send` recompares them, so any profile edit
    invalidates a stale context even at the same endpoint.
17. **Optional material reserves 4096 bytes for repair**; required material is not charged
    it. `repair_available` is explicit; a context without headroom is still sent, but its
    first structural failure is terminal.
18. **Pin acquisition is atomic over required and optional contributors.** Any contributor
    that expired or was purged between build and acquire denies the whole acquire
    (`PinDenied`), and #70 may rebuild once before the first send. No context is rewritten
    in place and no partially pinned context is sent.
19. **`InsufficientContext` is persistable too**: same `versions`/`scope`/`policy_digest`
    plus its own `digest` and `record()`, so #70 writes one `diagnosis_contexts` row for
    either outcome without a second schema or a nullable-message special case.

## 15. Open items for the builder

- Confirm the actual `features/diagnosis` module names at Task 0 against the merged #68
  (this document assumes `messages.py`, `profile.py`, `schemas.py`, `transport.py`).
- Add `messages.serialize_payload` as an additive refactor at Task 0 and prove
  `prepare_payload` still enforces `max_input_bytes` (existing R1 tests must stay green).
- Coordinate the additive E05 `run_scope` helper with @pho-veteran (no behaviour change).
- If #70 merges `diagnosis_contexts` first, align `record()` field names; do not add a
  migration or rename #70 fields.
