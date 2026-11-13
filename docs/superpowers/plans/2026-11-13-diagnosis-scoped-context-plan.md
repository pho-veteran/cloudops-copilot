# Scoped Diagnosis Context and Evidence-Bound Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement #69 — one bounded, policy-approved diagnosis context built from a persisted Run, a versioned five-family eligibility matrix, a deterministic required/optional target manifest, allowlist-checked messages for every send phase, strict `RCAAdvice/v1` parsing/validation/normalization against the sent refs, bounded structural repair feedback, and temporary analysis-owned evidence pins — with all five #69 acceptance tests proven.

**Architecture:** Additive modules in the existing `features/diagnosis/` feature (#68 R1 owns transport/profile/messages): `versions.py`, `egress.py`, `context.py`, `advice.py`, `pins.py`. Read-only composition over E04 (`investigations.storage/service`, `locators.project`, `comparison_service.citation`) and E05 (`knowledge.guidance`), plus one additive public serializer in `messages.py` and one additive `run_scope` helper in `knowledge.guidance`. **No Alembic migration, no HTTP operation, no OpenAPI/TypeScript change, no web fixture** — #70 owns persistence and exposure, #71 owns API journeys.

**Tech Stack:** Python 3 / SQLAlchemy / PostgreSQL (read-only queries and `evidence_pins` inserts), Pydantic v2, pytest; existing R1 `ModelProfile`/`Message`/`ModelCall` primitives.

**Spec:** `docs/superpowers/specs/2026-11-13-diagnosis-scoped-context-design.md` (authoritative on semantics and limits; this plan is authoritative on files, names and tests). Parent decisions: EPIC E06 #9, D1–D5.

---

## Global Constraints

- **Gate:** Task 0 must confirm #68 (R1) is merged to `origin/main` and record the exact module/test names before any other task. Branch from `origin/main` (local `main` may be stale), never from local `main` HEAD.
- **No migration, no exposed contract.** #69 adds no table, no column, no route, no permission-inventory entry and no generated transport artifact. `git status` after every task must show no `migrations/versions/`, `docs/api/`, `web/src/api/` or `tests/permission_inventory.py` change. If #70 merges a migration first, rebase; do not touch its revision.
- **Read-only context build.** `context.build` and `advice.validate` never `commit`, never mutate `InvestigationRun`/`Evidence`, and never open a socket. Only `pins.acquire`/`pins.release` write, and only `evidence_pins` rows with `kind="analysis"`.
- **One byte measurement.** `messages.serialize_payload(profile, messages)` (added in Task 0) is the only definition of payload size; `prepare_payload` delegates to it. Effective budget = `min(policy.max_message_bytes, profile.limits.max_input_bytes)` ≤ 65536 (D1).
- **Required material is never trimmed.** Rule caps (`e06.targets/v1`) and alias capacity (999) are pre-budget; a required overflow is an `InsufficientContext` with unchanged family matrix and no messages. Optional material is never split.
- **Repair reserve.** Optional material fills only up to `budget - 4096`; required material is not charged the reserve. `repair_available` is explicit; `feedback_message` returns `None` when the repaired payload would exceed the budget.
- **Deny patterns deny, never redact.** A denied required value is an insufficiency; a denied optional value is an omission. No matched bytes appear in any code, path, message, log or fixture.
- **Server-only authority.** Family, template, scope, UID, policy and budget are never accepted from HTTP or model output. `RCAAdvice/v1` has `extra="forbid"`; unknown fields are `schema_invalid`.
- **Evidence fidelity.** Use `Entry.value` (full) and `locators.project`; never `Entry.view()` (1 KiB preview) and never byte-truncate a value. Source coverage and sent coverage are separate and never merged.
- **English only** in code, comments, fixtures and docs.
- **Database tests** use `TEST_DATABASE_URL` ending in `_test`; never point tests at application data. Pure tests live in `services/backend/tests_unit/`, DB tests in `services/backend/tests/`.
- **Git identity** must be `pho-veteran` (AGENTS.md): verify `gh api user --jq .login`, verify author name/email, use command-scoped overrides. If it cannot be verified, do local work only and stop before any remote write.

## Review Focus

1. **Transport truth, not a parallel serializer.** If `message_bytes` is computed from anything other than `serialize_payload`, a context can pass the build and fail `prepare_payload` (or vice versa). → Task 0 helper, Task 5 `test_message_bytes_equals_transport_and_prepare_payload_accepts`, Task 6 `test_check_send_measures_with_serialize_payload`.
2. **Required-target fidelity.** A preview, a truncation, a rule-cap hidden as budget omission, or a shrunk family set is a silent evidence change. → Task 4 `test_full_value_not_preview`, `test_rule_caps_are_recorded_as_rule_omitted`; Task 5 `test_required_overflow_returns_insufficiency_without_messages` (asserting the family matrix is unchanged).
3. **Re-binding on every phase.** Retry/repair/probe must not reuse a stale policy or profile. → Task 6 `test_check_send_rejects_policy_digest_change`, `test_check_send_rejects_profile_identity_change`.
4. **Deny without leak.** The request text is the easiest place to leak a secret into a message *and* into an error string. → Task 6 `test_deny_pattern_in_request_text_fails_without_echo`, `test_sendcheck_code_contains_no_payload_text`.
5. **Pin atomicity and lock order.** A partially pinned context or a pin that resurrects purged content is a correctness failure, not a race to accept. → Task 9 `test_expired_optional_contributor_denies_whole_acquire`, `test_purge_before_pin_denies_without_resurrection`.

## Acceptance Coverage (issue #69 AC1–AC5)

| AC | Tasks | Proving tests |
| --- | --- | --- |
| **AC1** scope, selection independence, coverage/limitations, no family inference | 1, 3, 5 | `tests/test_diagnosis_context_selection.py` |
| **AC2** budget/omissions, required overflow, no eligible family, full values | 4, 5 | `tests_unit/test_diagnosis_context_build.py` |
| **AC3** egress allowlist/deny in initial, retry, repair, probe; no leak; injection | 2, 6 | `tests_unit/test_diagnosis_egress.py`, `tests_unit/test_diagnosis_advice.py` |
| **AC4** exact refs, invalid cases, partial vs ruled-out, fixtures not diagnosis | 7, 8 | `tests_unit/test_diagnosis_advice.py`, `tests/test_diagnosis_validation.py` |
| **AC5** independent selections, purge/pin race, terminal cleanup, structural ≠ semantic | 8, 9 | `tests/test_diagnosis_pins.py`, `tests/test_diagnosis_validation.py` |

---

### Task 0: Align with merged #68 and add the two shared helpers (gate — do first)

**Files:**
- Modify: `services/backend/src/cloudops/features/diagnosis/messages.py` (`serialize_payload`)
- Modify: `services/backend/src/cloudops/features/knowledge/guidance.py` (`run_scope`)
- Test: `services/backend/tests_unit/test_diagnosis_messages.py`
- Test: `services/backend/tests/test_run_guidance.py`
- Modify: this plan and the design doc (alignment notes: confirmed names only)

**Interfaces:**
- Produces: `messages.serialize_payload(profile: ModelProfile, messages) -> bytes`; `prepare_payload` = `serialize_payload` + the existing `max_input_bytes` check.
- Produces: `knowledge.guidance.run_scope(db, incident_id, run_id) -> KnowledgeRunScope | None` — delegates to the existing `_scope(db, run, None, None)`; no behaviour change.
- Records: exact names of the merged R1 modules and the test fixture helper (`services/backend/tests/diagnosis_fixtures.py`).

- [ ] **Step 0.1: Verify identity and baseline**

Run: `gh api user --jq .login`
Expected: exactly `pho-veteran`. If not, switch/re-verify; if still unavailable, do local work only and stop before any remote write.

Run: `git fetch origin && git log origin/main --oneline -5`
Expected: #68 R1 commits present (`ea5a42c` or later). If #68 is absent: stop and report.

- [ ] **Step 0.2: Confirm names and branch**

Create the branch from `origin/main` (e.g. `feat/diagnosis-scoped-context`). Read `services/backend/src/cloudops/features/diagnosis/` and `services/backend/tests/diagnosis_fixtures.py`; confirm the module names assumed by this plan (`messages.py`, `profile.py`, `schemas.py`, `transport.py`) and the fixture helpers. Write one "Task 0 alignment" paragraph into the design doc.

- [ ] **Step 0.3: Write the failing serializer test**

```python
# services/backend/tests_unit/test_diagnosis_messages.py (append)
from cloudops.features.diagnosis.messages import Message, prepare_payload, serialize_payload
from tests.diagnosis_fixtures import profile

def test_serialize_payload_is_the_transport_measurement():
    messages = [Message("system", "s"), Message("user", "u")]
    encoded = serialize_payload(profile(), messages)
    assert encoded == prepare_payload(profile(), messages)
    assert encoded.startswith(b'{"model":')

def test_prepare_payload_still_enforces_max_input_bytes():
    tiny = profile(limits={"max_input_bytes": 16})
    with pytest.raises(MessageError):
        prepare_payload(tiny, [Message("user", "x" * 64)])
```

- [ ] **Step 0.4: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_messages.py -v`
Expected: FAIL (`ImportError: serialize_payload`).

- [ ] **Step 0.5: Implement `serialize_payload` and `run_scope`**

`messages.py`: move the sendable-profile check, `_normalize`, body construction and `json.dumps(...).encode("utf-8")` (with the `UnicodeEncodeError` → `MessageError` mapping) into `serialize_payload`; `prepare_payload` calls it and then enforces `max_input_bytes`. No other change, and every existing R1 message test must stay green.

`guidance.py`: add

```python
def run_scope(db: Session, incident_id: UUID, run_id: UUID) -> KnowledgeRunScope | None:
    """Public read-only scope for trusted consumers; identical to internal resolution."""
    return _scope(db, _run(db, incident_id, run_id), None, None)
```

and make `_scope` the only implementation (no copied logic).

- [ ] **Step 0.6: Write the failing scope-delegation test**

```python
# services/backend/tests/test_run_guidance.py (append)
def test_run_scope_matches_internal_scope(db, seeded):
    # run with question, window and complete workload → run_scope(...) equals
    # the scope used by pin_guidance (same ids, scope labels, window);
    # legacy run (question NULL / not_captured) → None, not an exception.
```

- [ ] **Step 0.7: Run the R1 and E05 suites**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_messages.py services/backend/tests_unit/test_diagnosis_client.py services/backend/tests_unit/test_diagnosis_profile.py services/backend/tests_unit/test_diagnosis_transport.py services/backend/tests/test_run_guidance.py -v`
Expected: PASS for all, including the new tests.

- [ ] **Step 0.8: Commit**

```sh
git add services/backend/src/cloudops/features/diagnosis/messages.py services/backend/src/cloudops/features/knowledge/guidance.py services/backend/tests_unit/test_diagnosis_messages.py services/backend/tests/test_run_guidance.py docs/superpowers/specs/2026-11-13-diagnosis-scoped-context-design.md docs/superpowers/plans/2026-11-13-diagnosis-scoped-context-plan.md
git commit -m "feat(diagnosis): share payload measurement and run scope"
```

---

### Task 1: Version identities and versioned bounds

**Files:**
- Create: `services/backend/src/cloudops/features/diagnosis/versions.py`
- Test: `services/backend/tests_unit/test_diagnosis_context_build.py` (first section)

**Interfaces:**
- Produces constants used everywhere else (no imports from context/egress/advice):

```python
CONTEXT_VERSION = "e06.context/v1"
TARGET_RULES_VERSION = "e06.targets/v1"
PROMPT_VERSION = "e06.rca_prompt/v1"
VALIDATOR_VERSION = "e06.validator/v1"
PROBE_PROMPT_VERSION = "e06.probe_prompt/v1"
ALIAS_CAPACITY = 999
REPAIR_RESERVE_BYTES = 4096
MAX_FEEDBACK_BYTES = 2048
MAX_FEEDBACK_FAILURES = 10
SLOT_CAPS = {  # required cap, optional cap, ordering rule id
    "kubernetes.state": (250, 250, "key_asc"),
    "kubernetes.configmap.metadata": (250, 250, "key_asc"),
    "kubernetes.events": (50, 20, "newest_first"),
    "kubernetes.logs.current": (3, 1, "group_newest"),
    "kubernetes.logs.previous": (3, 1, "group_newest"),
    "prometheus.request_rate": (250, 250, "key_asc"),
    "prometheus.error_ratio": (250, 250, "key_asc"),
    "prometheus.p95_latency": (250, 250, "key_asc"),
    "prometheus.memory_working_set": (250, 250, "key_asc"),
    "prometheus.memory_limit": (250, 250, "key_asc"),
    "prometheus.restarts": (250, 250, "key_asc"),
    "development_fixture": (0, 20, "key_asc"),
}
SOURCE_CLASSES = {"kubernetes.state": "kubernetes_state", ...}
FAMILY_REQUIRED = {"crashloop_startup": (...), ...}  # D2 table, fixed order
```

- [ ] **Step 1.1: Write the failing tests**

```python
# tests_unit/test_diagnosis_context_build.py
def test_versions_are_unique_and_pinned():
    # exact strings for all five versions; every constant starts with "e06." and
    # ends with "/v1"; no duplicates.

def test_alias_and_reserve_bounds_match_the_advice_contract():
    # ALIAS_CAPACITY == 999; REPAIR_RESERVE_BYTES == 4096; MAX_FEEDBACK_BYTES == 2048 <= reserve.

def test_family_matrix_is_the_d2_table_in_fixed_order():
    # tuple(FAMILY_REQUIRED) == the five families in D2 order; crashloop/oom require
    # state, events, both logs; traffic requires the three split metrics;
    # pending/image_pull require state+events.

def test_every_slot_has_a_source_class_and_cap():
    # key sets of SOURCE_CLASSES and SLOT_CAPS are identical; development_fixture
    # has required cap 0.
```

- [ ] **Step 1.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py -v`
Expected: FAIL (`ModuleNotFoundError: cloudops.features.diagnosis.versions`).

- [ ] **Step 1.3: Implement `versions.py`**

Pure module: strings, tuples, dicts, no imports from other diagnosis modules, no side effects. Copy the D2 family table exactly; do not derive it from `investigations.catalog._PACKS` (that list is private and not a public contract).

- [ ] **Step 1.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/versions.py services/backend/tests_unit/test_diagnosis_context_build.py
git commit -m "feat(diagnosis): version the context, target and validator rules"
```

---

### Task 2: `EgressPolicy/v1` loading, digest and class gate

**Files:**
- Create: `services/backend/src/cloudops/features/diagnosis/egress.py` (policy half)
- Create: `config/diagnosis-egress.example.json`
- Modify: `.env.example` (comment only, next to `DIAGNOSIS_MODEL_CONFIG`)
- Test: `services/backend/tests_unit/test_diagnosis_egress.py`

**Interfaces:**
- Produces:
  - `EgressPolicy` (frozen, `extra="forbid"`): `schema`, `policy_id`, `policy_version`, `destination {config_id, config_version, endpoint_label}`, `allowed_source_classes: list[Literal[...]]`, `max_message_bytes`, `max_target_bytes`, `redaction_policy_version`, `deny_patterns: list[DenyPattern]`.
  - `EgressPolicy.load(path) -> EgressPolicy` / `load_optional(path) -> EgressPolicy | None`; `ProfileError`-style `EgressPolicyError`.
  - `policy.digest() -> str`; `policy.allows(source_class) -> bool`; `policy.destination_matches(identity: CallIdentity) -> bool`.
  - `policy.scan(text) -> str | None` returning the matching pattern **name** only (never the match or the text).

- [ ] **Step 2.1: Write the failing tests**

```python
# tests_unit/test_diagnosis_egress.py
def test_policy_denies_everything_by_default():
    # example file: allowed_source_classes==[]; allows("kubernetes_state") is False;
    # every class including diagnostic_fixture is denied.

def test_policy_rejects_unknown_fields_and_out_of_range_values():
    # extra key → ValidationError; max_message_bytes=65537 or 0 → error;
    # max_target_bytes > max_message_bytes → error; >20 deny patterns → error;
    # invalid regex → error that does not echo the pattern text.

def test_policy_digest_is_canonical_and_stable():
    # same JSON from two key orders → identical digest; changing any allowed field
    # changes the digest; destination digest changes when endpoint_label changes.

def test_destination_matches_exact_call_identity():
    # profile(...).identity() with matching config_version/endpoint_label → True;
    # different endpoint_label/config_version → False; a URL or credential field
    # is rejected by the policy model.

def test_missing_file_is_no_policy():
    # load_optional(None) / load_optional(missing path) → None; load(missing) raises.

def test_example_config_validates():
    # EgressPolicy.load("config/diagnosis-egress.example.json") passes and is the
    # deny-all policy referenced by docs.
```

- [ ] **Step 2.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_egress.py -v`
Expected: FAIL (`ModuleNotFoundError: cloudops.features.diagnosis.egress`).

- [ ] **Step 2.3: Implement the policy half of `egress.py`**

Mirror R1's profile conventions: bounded file size (≤64 KiB), UTF-8 JSON, `model_validate`, error strings that name only a field and a rule. Compile deny patterns once with `re.compile`, reject patterns >512 chars, and cap the number of patterns. `digest()` = SHA-256 of `json.dumps(model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()` — the digest covers `deny_patterns` too.

- [ ] **Step 2.4: Add the example config and env comment**

`config/diagnosis-egress.example.json` = `policy_id="example-deny-all"`, empty allowlist, 65536/8192, `redaction/v1`, the three deny patterns from the design. `.env.example`: `# DIAGNOSIS_EGRESS_POLICY=config/diagnosis-egress.example.json` with one line saying it is loaded by the trusted entrypoint (#70), never by a browser.

- [ ] **Step 2.5: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_egress.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/egress.py services/backend/tests_unit/test_diagnosis_egress.py config/diagnosis-egress.example.json .env.example
git commit -m "feat(diagnosis): add deny-by-default egress policy"
```

---

### Task 3: Server scope, source material and the five-family matrix

**Files:**
- Create: `services/backend/src/cloudops/features/diagnosis/context.py` (scope/material/matrix half)
- Test: `services/backend/tests/test_diagnosis_context_selection.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ContextScope:
    knowledge: KnowledgeRunScope
    request_text: str
    request_source: str          # operator | title_default | automatic
    window_source: str

@dataclass(frozen=True)
class SlotMaterial:
    slot: str
    source_class: str
    evidence_id: UUID | None
    evidence_sha256: str | None
    payload_version: int | None
    coverage_status: str         # available|partial|unavailable|not_applicable|missing
    coverage_reason_codes: tuple[str, ...]
    content_status: str          # available|unavailable|expired|missing
    time_relation: str
    entries: tuple[Entry, ...]   # non-ambiguous only, FULL Entry.value
    ambiguous_omitted: int

def resolve_scope(db, incident_id, run_id) -> ContextScope | None
def load_materials(db, scope: ContextScope) -> list[SlotMaterial]   # catalog order
def family_matrix(materials, policy) -> list[FamilyAssessment]
def usability(material, policy) -> tuple[bool, bool, list[str]]      # usable, waived, reasons
```

- [ ] **Step 3.1: Write the failing DB tests (AC1)**

```python
# tests/test_diagnosis_context_selection.py
def test_scope_matches_persisted_run_and_workload(db, seeded):
    # Run + Workload + Incident → ContextScope.ids equal rows; cluster/namespace/name/uid
    # and window match the Run; request_text/source/window_source copied verbatim.

def test_legacy_run_without_request_or_window_has_no_scope(db, seeded):
    # question NULL, question_source="not_captured", missing window, incomplete
    # workload → None. No exception, no fabricated scope.

def test_coverage_separates_source_and_content_status(db, seeded):
    # available / partial / unavailable / not_applicable (logs) / expired (payload NULL
    # and expires_at past, unpinned) / missing rows → distinct SlotMaterial values;
    # expired content is never projected.

def test_partial_is_usable_but_blocks_only_ruled_out(db, seeded):
    # state partial + events/logs available → crashloop matrix partial=True but
    # eligible_for_assessment=True; FamilyAssessment.partial propagates.

def test_development_fixture_never_satisfies_a_required_slot(db, seeded):
    # only development_fixture stored → every family eligible==False, no evidence
    # targets, reason includes synthetic_fixture_not_production_evidence.

def test_missing_traffic_leaves_kubernetes_families_eligible(db, seeded):
    # state+events+logs available, no prometheus slots → crashloop/oom/pending/image_pull
    # eligible; traffic ineligible with required_slot_unavailable reasons.

def test_ambiguous_entries_are_counted_and_excluded(db, seeded):
    # a payload with a repeated identity and different values → ambiguous_omitted>0,
    # entries contains no ambiguous Entry, slot still usable if another entry remains.

def test_outside_window_is_a_limitation_not_a_closed_interval(db, seeded):
    # time_relation="extends_after_window" → material records that relation; the
    # resulting context limitation names it and no invented window is produced.

def test_no_family_is_inferred_from_question_or_pack_coverage(db, seeded):
    # question mentions "CrashLoopBackOff", stored pack_coverage says crashloop
    # available → matrix is derived only from slot usability; no family is selected.
```

- [ ] **Step 3.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_context_selection.py -v`
Expected: FAIL (`ModuleNotFoundError: cloudops.features.diagnosis.context`).

- [ ] **Step 3.3: Implement scope and materials**

`resolve_scope` calls `knowledge.guidance.run_scope` (Task 0) and adds `request_text`, `request_source`, `window_source` read from the same `InvestigationRun` row. `load_materials` selects `Evidence` for the Run, calls `investigations.storage.evidence_views(db, rows)` for `content_status`, maps `source_key`→class via `versions.SOURCE_CLASSES`, and, only for `content_status=="available"`, calls `locators.project(payload_kind, payload_version, payload)` (catch `UnsupportedProjection` → `projection_unsupported`, never raise). Iterate every slot in `versions.SLOT_CAPS` so missing slots are represented. Split non-ambiguous entries; keep full values.

- [ ] **Step 3.4: Implement usability and the matrix**

`usability` applies §7.2 exactly: `not_applicable` waives only the two log slots; `partial` is usable; `expired`/`unavailable`/`projection_unsupported`/`policy_denied` are not; `entries` empty after projection is unusable. `family_matrix` evaluates all five in `versions.FAMILY_REQUIRED` order, sets `eligible_for_assessment` only when every required slot is usable or waived **and** the class is allowed **and** the rule-based required set is non-empty (Task 4 extraction is not needed for the boolean: any non-ambiguous entry satisfies it), and fills `reasons` with closed enum strings. `development_fixture` is never consulted for a required slot.

- [ ] **Step 3.5: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_context_selection.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/context.py services/backend/tests/test_diagnosis_context_selection.py
git commit -m "feat(diagnosis): derive run scope and family eligibility matrix"
```

---

### Task 4: Target extraction, ordering and alias assignment (`e06.targets/v1`)

**Files:**
- Modify: `services/backend/src/cloudops/features/diagnosis/context.py` (target half)
- Test: `services/backend/tests_unit/test_diagnosis_context_build.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Extracted:
    slot: str
    entries: tuple[Entry, ...]
    rule_omitted: int
    ambiguous_omitted: int

def extract_required(material: SlotMaterial) -> Extracted
def extract_optional(material: SlotMaterial) -> Extracted
def required_targets(materials, families) -> list[ContextTarget]   # deduped, ordered
def optional_targets(materials, families, used: set[tuple]) -> list[ContextTarget]
```

- [ ] **Step 4.1: Write the failing tests (AC2, fidelity half)**

```python
# tests_unit/test_diagnosis_context_build.py
def test_state_cap_is_250_in_key_order():
    # 260 valid entries → 250 kept, rule_omitted=10, order == sorted(entry.key).

def test_events_are_newest_first_and_capped():
    # events with lastTimestamp/eventTime/creationTimestamp out of order → newest
    # first; missing timestamps fall back to entry.key; required cap 50, optional 20.

def test_logs_keep_three_newest_segments_per_group():
    # two (pod,container,previous) groups × 5 segments → 3 per group; identity
    # timestamp parsed from the log identity; missing timestamp → key order.

def test_metrics_include_series_and_samples_with_cap():
    # series + samples → entries in key order, cap 250, rule_omitted recorded.

def test_development_fixture_is_never_required_and_optional_capped():
    # required extract returns no targets for development_fixture; optional keeps 20.

def test_required_union_dedupes_across_families_and_orders_aliases():
    # state/events shared by crashloop and pending → one target, first alias wins;
    # aliases are E001.. in D2 family order, slot order, rule order.

def test_optional_targets_continue_aliases_in_catalog_order():
    # optional configmap/other metrics/logs follow the required aliases; no gaps.

def test_full_value_not_preview():
    # a 2 KiB state value (entry.view() would truncate) → ContextTarget value bytes
    # equal the canonical full value; no "…" and no preview_truncated flag in context.

def test_rule_caps_are_recorded_as_rule_omitted():
    # rule_omitted is a separate counter from budget omission; a capped slot still
    # counts as usable/eligible.
```

- [ ] **Step 4.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py -v`
Expected: FAIL (`AttributeError: extract_required`).

- [ ] **Step 4.3: Implement extraction and aliases**

Ordering helpers: `_timestamp(entry)` checks `entry.value` keys in the documented order, then `locators.timestamp()`, then `entry.key`. For logs, parse the identity list `[pod, container, previous, timestamp, digest]` from `entry.identity` (canonical JSON); if it is not that shape, treat the entry as its own group keyed by `entry.key`. Keep `ContextTarget` construction in one function that copies the full `Entry.value`, computes `entry_digest = digest(value)`, and never calls `Entry.view()`. Dedupe key is `(evidence_id, locator.kind, locator.key)`; preserve first occurrence. Assign aliases with `f"E{index:03d}"` and assert `index <= versions.ALIAS_CAPACITY`.

- [ ] **Step 4.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py -v`
Expected: PASS (Task 1 tests still pass).

```sh
git add services/backend/src/cloudops/features/diagnosis/context.py services/backend/tests_unit/test_diagnosis_context_build.py
git commit -m "feat(diagnosis): extract and order full-fidelity context targets"
```

---

### Task 5: Deterministic assembly, budget, `PreparedContext` and insufficiency records

**Files:**
- Modify: `services/backend/src/cloudops/features/diagnosis/context.py` (assembly + records + `build`)
- Test: `services/backend/tests_unit/test_diagnosis_context_build.py`
- Test: `services/backend/tests/test_diagnosis_context_selection.py` (one DB `build` test)

**Interfaces:**
- Produces:

```python
class SourceCoverage(BaseModel): ...      # slot, source_class, status, reasons, evidence_id
class SentCoverage(BaseModel): ...        # per-slot included/rule/budget/ambiguous/bytes + totals
class ContextTarget(BaseModel): ...
class ContextRunbook(BaseModel): ...
class Omission(BaseModel): ...            # code, slot, alias|None, count
class FamilyAssessment(BaseModel): ...

class PreparedContext(BaseModel):
    ...
    def record(self) -> dict
    @property digest: str

class InsufficientContext(BaseModel):
    ...
    def record(self) -> dict
    @property digest: str

def assemble(
    materials, scope, selection, policy, profile, analysis_identity
) -> PreparedContext | InsufficientContext

def build(
    db, incident_id, run_id, *,
    selection: KnowledgeSelectionView | None,
    policy: EgressPolicy,
    profile: ModelProfile,
    analysis_identity: str,
):
    scope = resolve_scope(...)
    if scope is None: return InsufficientContext(reason="scope_unavailable", ...)
    return assemble(load_materials(db, scope), scope, selection, policy, profile, analysis_identity)
```

`selection` is `RunGuidance.selection` from #70's E05 `pin_guidance(consumer="analysis",
request_identity=<analysis id>)` call; `None` (E05 `state="unavailable"`) yields
`runbook_status="not_selected"` plus a limitation and never blocks the build. Runbook
material is `selection.retrieval.chunks` (each `GuidanceChunk` carries `reference`,
`title`, `heading_path`, `content`); only `status in {"matched","partial"}` chunks are
eligible, in retrieval rank order.

- [ ] **Step 5.1: Write the failing pure tests (AC2)**

```python
# tests_unit/test_diagnosis_context_build.py
def test_effective_budget_uses_the_lower_limit():
    # policy 65536 + profile max_input_bytes=10000 → effective_budget_bytes==10000;
    # policy 1000 + profile 65536 → 1000.

def test_message_bytes_equals_transport_and_prepare_payload_accepts():
    # message_bytes == len(serialize_payload(profile, ctx.messages));
    # prepare_payload(profile, ctx.messages) returns the same bytes (no exception).

def test_optional_is_omitted_greedily_under_the_repair_reserve():
    # optional values that fit only inside the last 4096 bytes are omitted with
    # budget_omitted; message_bytes <= budget - 4096; repair_available is True.

def test_required_filling_the_budget_sets_repair_unavailable():
    # required union consumes all but <4096 bytes → optional empty, repair_available
    # False, context still returned (no insufficiency).

def test_required_overflow_returns_insufficiency_without_messages():
    # required union + envelope > budget → InsufficientContext(
    # required_targets_exceed_budget), no messages field, family matrix unchanged
    # (all previously eligible families remain eligible).

def test_request_exceeds_budget_is_terminal():
    # a question larger than the envelope budget → request_exceeds_budget.

def test_alias_capacity_is_999_required():
    # 1000 minimal required targets → required_alias_space_exceeded; 999 → built.

def test_request_text_is_envelope_only_and_never_citable():
    # request appears once in the user payload; no ContextTarget/alias refers to it;
    # RCAResult cannot cite it (validator tested in Task 7).

def test_insufficiency_has_digest_and_record_shape():
    # record() for both variants has versions/scope/policy_digest/outcome/digest;
    # insufficient has message_bytes None; record() never contains target value text
    # or the request text.

def test_rebuild_is_byte_identical_and_digest_stable():
    # assemble twice from frozen inputs → identical messages, message_bytes, digest;
    # changing policy_version or target_rules_version changes the digest.
```

- [ ] **Step 5.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py -v`
Expected: FAIL (`ImportError: assemble`).

- [ ] **Step 5.3: Implement records and assembly**

Canonical user payload = `json.dumps({...}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` with a fixed key order (request, scope, versions, source_coverage, families, required_targets, optional_targets, runbooks, limitations). Build the system message from `PROMPT_VERSION`; measure every candidate with `serialize_payload` (Task 0). Steps: scope check → request checks (missing → `request_not_captured`; deny → `request_content_denied`; too large → `request_exceeds_budget`) → matrix → required union (cap/alias check) → fit check → optional greedy under `budget - REPAIR_RESERVE_BYTES` → runbooks (same reserve, `selection.retrieval.chunks` in rank order, referencing `selection.id`) → `message_bytes`/`content_bytes`/`digest` → `record()`. `InsufficientContext` is returned at the first terminal reason with the full coverage/matrix/omissions gathered so far, its own canonical digest, and no messages.

- [ ] **Step 5.4: Add the DB integration test**

```python
# tests/test_diagnosis_context_selection.py
def test_build_over_persisted_run_matches_the_pure_assembly(db, seeded):
    # build() over stored evidence + analysis selection equals assemble() on the
    # materials read in the same transaction; scope ids and digest match; a legacy
    # run returns scope_unavailable without reading evidence payloads.

def test_analysis_selection_is_independent_of_manual_selection(db, seeded):
    # manual selection saved as no_matches; analysis pin_guidance(consumer="analysis",
    # request_identity="analysis-1") returns matched chunks → build uses the analysis
    # selection id/chunks; replacing the manual selection afterwards changes neither
    # the analysis selection nor the rebuilt context digest.
```

- [ ] **Step 5.5: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_context_build.py services/backend/tests/test_diagnosis_context_selection.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/context.py services/backend/tests_unit/test_diagnosis_context_build.py services/backend/tests/test_diagnosis_context_selection.py
git commit -m "feat(diagnosis): assemble bounded deterministic diagnosis context"
```

---

### Task 6: Prompt, phase send checks and the probe

**Files:**
- Modify: `services/backend/src/cloudops/features/diagnosis/egress.py` (`check_send`, `prepare_probe`, `SendCheck`)
- Modify: `services/backend/src/cloudops/features/diagnosis/context.py` (`SYSTEM_PROMPT` constant only)
- Test: `services/backend/tests_unit/test_diagnosis_egress.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class SendCheck:
    ok: bool
    messages: tuple[Message, ...] | None
    digest: str | None
    byte_size: int | None
    code: str | None       # closed enum, no payload text

Phase = Literal["initial", "retry", "repair"]

def check_send(context, policy, profile, phase, *, feedback=None) -> SendCheck
def prepare_probe(policy, profile) -> SendCheck   # ok=False codes: policy/identity
def probe_expected_text() -> str                  # "OK"
```

- [ ] **Step 6.1: Write the failing tests (AC3)**

```python
# tests_unit/test_diagnosis_egress.py
def test_check_send_rejects_policy_digest_change():
    # context built under policy A, check under policy B (same version, different
    # allowlist) → ok=False, code="policy_changed", messages=None.

def test_check_send_rejects_profile_identity_change():
    # same config triple, different behavior_digest (model or limits edited) →
    # ok=False, code="profile_identity_changed".

def test_check_send_rejects_unallowlisted_class():
    # context contains kubernetes_state but policy allows only kubernetes_events →
    # ok=False, code="source_class_denied"; no bytes of the message returned.

def test_deny_pattern_in_request_text_fails_without_echo():
    # request contains "password=hunter2hunter2" → ok=False, code="content_denied";
    # repr(SendCheck) and its fields contain no match, no request text.

def test_deny_pattern_in_target_value_fails_without_echo():
    # stored evidence value contains a bearer token → same; failure carries only
    # the pattern name/code, never the value.

def test_initial_and_retry_use_identical_messages_and_are_both_rechecked():
    # phase="initial" and phase="retry" return the same messages/digest/byte_size;
    # a policy change between them flips the second to ok=False.

def test_repair_appends_only_structural_codes_and_paths():
    # feedback built from failures → ok=True; user content prefix equals the
    # initial content; appended block lists only code+path, max 10 lines/2048 bytes.

def test_repair_refused_when_no_headroom():
    # context with repair_available=False → check_send(phase="repair") →
    # ok=False, code="repair_unavailable"; no message returned.

def test_probe_requires_diagnostic_fixture_and_matching_identity():
    # deny-all → ok=False code="probe_not_allowed"; allowlist without
    # diagnostic_fixture → probe_not_allowed; allowed+match → fixed system/user
    # pair asking for "OK", expected text "OK", no Run/evidence read.

def test_sendcheck_code_contains_no_payload_text():
    # every failure code is a member of the closed enum; no code contains a
    # substring of the request, a target value, a provider body or a credential.

def test_check_send_measures_with_serialize_payload():
    # ok SendCheck.byte_size == len(serialize_payload(profile, check.messages)) and
    # <= context.effective_budget_bytes; a budget lowered after build →
    # ok=False code="budget_exceeded" with messages=None.
```

- [ ] **Step 6.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_egress.py -v`
Expected: FAIL (`AttributeError: check_send`).

- [ ] **Step 6.3: Implement `check_send` and `prepare_probe`**

Rebuild the phase messages from `context.messages` (never from caller strings). Repair takes `feedback: str | None` produced by `advice.feedback_message`; enforce `MAX_FEEDBACK_BYTES`/`MAX_FEEDBACK_FAILURES` here too as defence in depth. Compare `policy.digest()`/`policy_version`, `profile.identity()`, manifest classes (including `operator_request`), scan `request.text` + every target value + every runbook content + the feedback block; measure with `serialize_payload`; assert one system then one user. `prepare_probe` uses only `PROBE_PROMPT_VERSION` constants.

- [ ] **Step 6.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_egress.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/egress.py services/backend/src/cloudops/features/diagnosis/context.py services/backend/tests_unit/test_diagnosis_egress.py
git commit -m "feat(diagnosis): allowlist every diagnosis send phase"
```

---

### Task 7: `RCAAdvice/v1` parsing, structural validation and normalization

**Files:**
- Create: `services/backend/src/cloudops/features/diagnosis/advice.py`
- Test: `services/backend/tests_unit/test_diagnosis_advice.py`

**Interfaces:**
- Produces:

```python
class RCAAdvice(BaseModel): ...        # frozen, extra="forbid", D2 shapes
class RCAResult(BaseModel): ...        # normalized, extra="forbid"
class ValidationFailure(BaseModel): code, path, repairable
class ValidationOutcome(BaseModel): status, result, failures, repairable

def parse(raw_text: str) -> RCAAdvice | list[ValidationFailure]
def validate_structural(context, advice) -> list[ValidationFailure]   # pure
def normalize(context, advice, resolver) -> RCAResult               # pure
def feedback_message(profile, context, failures) -> str | None     # pure, bounded
def validate(db, context, raw_text) -> ValidationOutcome            # Task 8 adds resolution
```

`resolver` is a callable `(alias) -> ResolvedRef | ValidationFailure`; the pure tests inject a fake, Task 8 injects the DB resolver.

- [ ] **Step 7.1: Write the failing tests (AC3/AC4, pure half)**

```python
# tests_unit/test_diagnosis_advice.py
def test_supported_fixture_normalizes_in_advice_order():
    # golden supported advice + fake resolver → RCAResult with resolved refs in
    # the advice's order, version identities, machine_validation="structural_v1",
    # quality="not_established".

def test_insufficient_fixture_normalizes_with_no_candidate():
    # status="insufficient_evidence", cause="unknown", candidate=None → valid result;
    # quality remains "not_established".

def test_extra_fields_are_rejected():
    # each of scope, model, template_id, parameters, endpoint added to the advice →
    # schema_invalid; the failure path names the field, no value echoed.

def test_unknown_duplicate_and_wrong_class_aliases():
    # E999 not in manifest → unknown_alias; duplicate in one list → duplicate_alias;
    # K001 in supporting → alias_wrong_class; E001 in runbook_citation_ids →
    # alias_wrong_class.

def test_supported_requires_eligible_family_and_matching_family_evidence():
    # cause=image_pull when only traffic is eligible → supported_requires_family_cause;
    # cause=crashloop with only traffic alias → supported_requires_family_evidence.

def test_insufficient_requires_unknown_cause_and_null_candidate():
    # sufficient status with cause=unknown → invalid; insufficient with a candidate →
    # insufficient_requires_unknown_cause / candidate_not_allowed.

def test_candidate_matrix():
    # crashloop→restore_deployment_template and oom→set_memory_profile and
    # traffic→scale_deployment valid; wrong mapping → candidate_mismatch; any
    # candidate on pending/image_pull/unknown → candidate_not_allowed.

def test_ruled_out_requires_family_coverage():
    # missing one required slot's alias → ruled_out_coverage_incomplete; alias from
    # another family → ruled_out_evidence_outside_family; empty ids → schema_invalid.

def test_partial_coverage_blocks_ruled_out_but_not_supported():
    # a partial slot in the family → ruled_out_partial_coverage for ruled_out;
    # a supported finding citing that family's aliases remains valid.

def test_waived_log_slot_does_not_block_ruled_out():
    # logs.previous not_applicable → ruled_out for state/events/logs.current passes
    # and adds limitation ruled_out_slot_waived:kubernetes.logs.previous.

def test_runbook_only_support_is_rejected():
    # supported finding with empty supporting_evidence_ids and only
    # runbook_citation_ids → runbook_only_support; empty support with no runbook
    # citations → supported_requires_family_evidence. A K alias in a telemetry
    # field is alias_wrong_class (schema-level, tested above).

def test_empty_next_steps_is_invalid():
    # next_steps=[] → empty_next_steps (or schema_invalid via min_length=1).

def test_feedback_is_structural_only_and_bounded():
    # failures with paths → block has code+path only, at most 10 lines/2048 bytes,
    # no alias names beyond paths, no value text; non-repairable codes are omitted.

def test_feedback_returns_none_when_it_cannot_fit():
    # context at the budget edge → feedback_message returns None; caller must not
    # attempt a repair (repair_available False).
```

- [ ] **Step 7.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_advice.py -v`
Expected: FAIL (`ModuleNotFoundError: cloudops.features.diagnosis.advice`).

- [ ] **Step 7.3: Implement `advice.py`**

Pydantic models exactly as design §6.5/§6.6 (`extra="forbid"`, frozen, list caps 20, alias regexes `^E[0-9]{3}$`/`^K[0-9]{3}$`). `parse` rejects non-object JSON and non-UTF-8-safe text as `json_invalid`; Pydantic errors become `schema_invalid` with the dotted path only. `validate_structural` runs the §10 order (membership → status → candidate → ruled-out → next_steps) with the closed code enum. `normalize` copies resolved refs and the sent target's `time_relation`/`coverage_status`; it never adds a family, parameter or action. `feedback_message` builds the fixed ASCII block, truncates to the cap, and measures with `serialize_payload`; returns `None` when it does not fit.

- [ ] **Step 7.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests_unit/test_diagnosis_advice.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/advice.py services/backend/tests_unit/test_diagnosis_advice.py
git commit -m "feat(diagnosis): validate and normalize evidence-bound advice"
```

---

### Task 8: Exact-reference validation against sent targets and runbooks

**Files:**
- Modify: `services/backend/src/cloudops/features/diagnosis/advice.py` (`validate` + DB resolver)
- Test: `services/backend/tests/test_diagnosis_validation.py`

**Interfaces:**
- Produces: `validate(db, context, raw_text) -> ValidationOutcome` using `investigations.comparison_service.citation(db, incident_id, run_id, evidence_id, kind, key)` and `knowledge.guidance.resolve_selection_chunk(db, incident_id, run_id, selection_id, chunk_id, document_version_id)`. Any mismatch is terminal (`repairable=False`).

- [ ] **Step 8.1: Write the failing DB tests (AC4/AC5)**

```python
# tests/test_diagnosis_validation.py
def test_supported_output_resolves_exact_evidence_and_runbook_refs(db, seeded):
    # real stored evidence + pinned analysis selection → valid result; each
    # CitationRef matches evidence_id/locator; each KnowledgeRef matches the pinned
    # version/chunk hashes; time_relation/coverage copied.

def test_swapped_locator_key_is_terminal(db, seeded):
    # advice cites E001 but the key belongs to E002's target → evidence_reference_mismatch,
    # repairable=False; no repair message.

def test_swapped_evidence_sha256_is_terminal(db, seeded):
    # evidence payload replaced after build → evidence_hash_mismatch (or
    # evidence_expired when purged).

def test_alias_from_another_run_does_not_exist(db, seeded):
    # evidence from Run B has no alias in Run A's manifest → unknown_alias,
    # terminal; no cross-Run resolution attempt escapes scope.

def test_expired_or_unavailable_evidence_is_terminal(db, seeded):
    # unpinned expired evidence → evidence_expired/evidence_unavailable,
    # repairable=False; validation never resurrects it.

def test_swapped_runbook_chunk_and_hash_are_terminal(db, seeded):
    # K001 mapped to a different chunk_id or document_version_id → 
    # runbook_reference_mismatch; a K alias not in the manifest → runbook_alias_unknown.

def test_pinned_old_runbook_resolves_after_reindex(db, seeded):
    # pin selection V1, publish V2, validate a K alias from V1 → resolves V1 exactly;
    # no current-version substitution.

def test_fixture_only_run_is_insufficient_and_never_supported(db, seeded):
    # Run with only development_fixture → context is InsufficientContext(no_eligible_family);
    # validate is never called; a handcrafted supported advice has no eligible family
    # and no aliases.

def test_validation_is_read_only(db, seeded):
    # captured row counts/versions before and after validate() are identical; no
    # diagnosis_* table is touched (none exists) and no EvidencePin is added.
```

- [ ] **Step 8.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_validation.py -v`
Expected: FAIL (no such function/route).

- [ ] **Step 8.3: Implement the DB resolver and `validate`**

For each alias: find the sent `ContextTarget`; call `comparison_service.citation`; assert `target.entry_digest == citation target digest` (recompute from the resolved value with `locators.digest`), `target.evidence_sha256 == stored sha256`, `source_key` equality, supported `payload_version`, non-ambiguous membership. For runbooks: call `resolve_selection_chunk` and compare all `KnowledgeRef` hashes. Convert `citation` 404/409 into `evidence_reference_mismatch`/`evidence_expired` without echoing payload text. Map Phase/`CallKind` policy is #70's; #69 only validates text.

- [ ] **Step 8.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_validation.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/advice.py services/backend/tests/test_diagnosis_validation.py
git commit -m "feat(diagnosis): resolve advice aliases to exact sent references"
```

---

### Task 9: Temporary analysis pins and purge/expiry races

**Files:**
- Create: `services/backend/src/cloudops/features/diagnosis/pins.py`
- Test: `services/backend/tests/test_diagnosis_pins.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class PinDenied:
    reason: Literal["contributing_content_unavailable"]
    slot: str
    alias: str | None

PinResult = Literal["acquired"] | PinDenied

def acquire(db, analysis_id: UUID, context: PreparedContext) -> PinResult
def release(db, analysis_id: UUID) -> int
```

- [ ] **Step 9.1: Write the failing DB tests (AC5)**

```python
# tests/test_diagnosis_pins.py
def test_acquire_pins_every_contributing_row(db, seeded):
    # required + optional targets → one pin per distinct evidence id, kind="analysis",
    # owner_id==analysis_id; all rows content_status=="available" while pinned.

def test_second_acquire_is_idempotent(db, seeded):
    # calling acquire twice → same pin count, no IntegrityError, no duplicate rows.

def test_expired_optional_contributor_denies_whole_acquire(db, seeded):
    # expire an evidence row used only by an optional target after build → 
    # PinDenied(slot, alias), zero analysis pins inserted.

def test_purge_before_pin_denies_without_resurrection(db, seeded):
    # purge_expired clears payload first → acquire denies; content_status stays
    # expired/unavailable; no pin, no payload restored.

def test_pin_before_purge_keeps_content(db, seeded):
    # acquire then purge_expired → pinned row keeps payload and stays available;
    # unrelated expired rows are still purged.

def test_release_preserves_latest_and_resolution_owners(db, seeded):
    # incident latest pin + resolution pin + analysis pin → release(analysis_id)
    # deletes only analysis rows; latest/resolution rows and content remain.

def test_double_release_is_safe(db, seeded):
    # release twice → second returns 0; no error; other owners untouched.

def test_release_then_expiry_is_explicit(db, seeded):
    # after release and expires_at in the past → content_status=="expired";
    # evidence_content raises 410; no resurrection, no silent success.

def test_analysis_pin_does_not_change_pack_coverage_or_run(db, seeded):
    # Run/Evidence columns unchanged (only evidence_pins rows added); locking order
    # is ascending Evidence.id (assert statement order via SQL capture or by
    # concurrent purge not deadlocking).
```

- [ ] **Step 9.2: Run to verify failure**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_pins.py -v`
Expected: FAIL (`ModuleNotFoundError: cloudops.features.diagnosis.pins`).

- [ ] **Step 9.3: Implement `pins.py`**

Collect distinct evidence ids from `context.required_targets + context.optional_targets`; `select(Evidence).where(Evidence.id.in_(ids)).order_by(Evidence.id).with_for_update(of=Evidence)`; re-read `evidence_views` under the lock; if any `content_status != "available"` return `PinDenied` before any insert; else insert missing `EvidencePin(evidence_id, kind="analysis", owner_id=analysis_id)` rows and `flush()`. Never commit (caller owns the transaction). `release` deletes only `kind="analysis" AND owner_id==analysis_id` and returns the deleted count.

- [ ] **Step 9.4: Verify and commit**

Run: `uv run --project services/backend pytest services/backend/tests/test_diagnosis_pins.py -v`
Expected: PASS.

```sh
git add services/backend/src/cloudops/features/diagnosis/pins.py services/backend/tests/test_diagnosis_pins.py
git commit -m "feat(diagnosis): pin analysis evidence for the context lifetime"
```

---

### Task 10: Documentation, deterministic fixtures and evidence record

**Files:**
- Create: `docs/diagnosis-context.md`
- Create: `docs/evidence/e06-r2-context/prepared-context.json`
- Create: `docs/evidence/e06-r2-context/advice-supported.json`
- Create: `docs/evidence/e06-r2-context/advice-insufficient.json`
- Create: `docs/evidence/e06-r2-context/invalid-cases.json`
- Create: `docs/evidence/e06-r2-context/egress-denials.json`
- Modify tests to load and validate the fixtures (`tests_unit/test_diagnosis_context_build.py`, `tests_unit/test_diagnosis_egress.py`, `tests_unit/test_diagnosis_advice.py`)
- Modify: `README.md` (one pointer to `docs/diagnosis-context.md`, next to `docs/diagnosis-model.md`)

**Interfaces:**
- Produces: a model-free producer/consumer record: every fixture is validated by the Pydantic schema and by the validator/egress code in the same test run.

- [ ] **Step 10.1: Write `docs/diagnosis-context.md`**

Sections: purpose and non-claims; D2 source classes and the five-family table; version identities; required/optional extraction caps and alias rules; effective budget and repair reserve; omit/insufficiency reason enum; send-phase allowlist and deny semantics; validation code enum; temporary pin lifecycle; explicit statements that structural validation ≠ semantic relevance, no execution authority, no universal secret detection.

- [ ] **Step 10.2: Create the fixtures from real builders**

Generate with a short script or by copying the test builders' canonical output; never hand-edit hashes. Each file carries a `schema` field (`PreparedContext/v1`, `RCAAdvice/v1`, `E06InvalidCaseMatrix/v1`, `E06EgressDenialMatrix/v1`).

- [ ] **Step 10.3: Add fixture-contract tests**

```python
def test_prepared_context_fixture_matches_the_model():
    # docs/evidence/e06-r2-context/prepared-context.json → PreparedContext.model_validate;
    # digest recomputed from the file equals the stored digest; record() has no
    # request/target value text.

def test_invalid_case_matrix_matches_the_validator_codes():
    # every {advice, context, expected_codes} row → validate_structural returns
    # exactly those codes (or absence for the valid row); no code outside the enum.

def test_egress_denial_matrix_matches_sendcheck_codes():
    # every {allowlist, request, values, expected_code} row → check_send/scan returns
    # that code and no field contains the matched text.
```

- [ ] **Step 10.4: Run the whole verification set**

```sh
uv run --project services/backend ruff format --check --config services/backend/pyproject.toml services/backend scripts
uv run --project services/backend ruff check --config services/backend/pyproject.toml services/backend scripts
uv run --project services/backend pytest services/backend/tests_unit -q
# with TEST_DATABASE_URL set to a dedicated database ending in _test:
uv run --project services/backend pytest services/backend/tests/test_diagnosis_context_selection.py services/backend/tests/test_diagnosis_validation.py services/backend/tests/test_diagnosis_pins.py -q
uv run --project services/backend pytest services/backend/tests -q
git status --short   # must show no migrations, no generated contracts, no permission inventory
```

Expected: all PASS. Record commit, environment, focused/full test output and the fixture matrix in the PR body per `CONTRIBUTING.md`.

- [ ] **Step 10.5: Commit**

```sh
git add docs/diagnosis-context.md docs/evidence/e06-r2-context services/backend/tests_unit/test_diagnosis_context_build.py services/backend/tests_unit/test_diagnosis_egress.py services/backend/tests_unit/test_diagnosis_advice.py README.md
git commit -m "docs(diagnosis): publish scoped context contracts and evidence"
```

---

## Final Verification Checklist

- [ ] All five #69 acceptance tests mapped to named tests (table above) and green.
- [ ] `message_bytes == len(serialize_payload(profile, messages))` and `prepare_payload` accepts every built context.
- [ ] No `Entry.view()` call and no byte truncation in `diagnosis/` (`grep -R "\.view()"`).
- [ ] No required-target trim, no family-set shrink, no silent redaction paths.
- [ ] Deny/insufficiency/validation codes are closed enums; no payload text in codes, paths, logs or fixtures.
- [ ] `git status` shows no migration, no API route, no generated contract, no web fixture.
- [ ] Docs state structural validation ≠ semantic relevance and that diagnosis never approves or executes.
- [ ] Git author/committer belong to `pho-veteran`; remote only if identity is verified.
