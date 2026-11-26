# Diagnosis engine: data and evaluation protocol

Part of the [engine improvement loop](README.md).

## Data

### Case generation (exists, with one fix to build)

`scripts/manage-local-lab.py fault <scenario>` supports `crashloop`, `oom`,
`traffic`, `pending`, `imagepull`, `init-retry` and the recovery faults `memory-limit`,
`bad-rollout` and `overload`. The scenario catalog and sealed oracle are
`config/lab/release-scenarios-v1.json` and `services/backend/tests/fixtures/e10_s3/`.

Per case: reset, then a baseline period, inject, wait for detection, collect through
the normal API and worker (diagnosis disabled), export, and reset.

**Required fix (to build):** reset must remove namespace events, and the next injection
must start after the previous window has closed, so no trial sees another trial's
evidence. Record the reset receipt with the bundle.

Variants to generate for each fault kind:

- clean
- event spam from an unrelated object
- unrelated Pod restart
- benign noisy logs
- untrusted instruction text in logs
- shifted window (fault near the window edge)

Negatives: healthy, insufficient evidence (source disabled), out of scope, and the
injection-only case.

Existing synthetic set: `services/backend/tests/fixtures/e06/cases.json`, 12 cases
(`c01`-`c10`, `n-healthy`, `n-injection`) with `oracle.json`. This set is T0 smoke and
regression only.

### RawEvidenceBundle/v1 (to build)

`EvalBundle/v1` freezes a prepared context, so it cannot test new selection or encoding.
The loop needs the evidence from *before* selection:

```json
{
  "schema_version": "RawEvidenceBundle/v1",
  "case_id": "oom-clean-007",
  "captured_at": "...",
  "source": {"kind": "lab|fixture|r3", "lab_project": "...", "source_revision": "..."},
  "scope": {"cluster": "...", "namespace": "...", "deployment": "...", "uid": "...",
            "window": {"start": "...", "end": "..."}, "request_text": "..."},
  "slots": [{"slot": "kubernetes.events", "coverage_status": "available",
             "time_relation": "within_requested_window", "payload_kind": "...",
             "payload_version": "...", "payload": {}, "sha256": "..."}],
  "reset_receipt_sha256": "...",
  "manifest_sha256": "..."
}
```

Labels live in a separate file using the existing `EvalLabel` model
(`evaluation/schemas.py`), keyed by `case_id`. Labels are never inside the bundle.

Exporter: read the Run's evidence rows through the same projection that
`load_materials` uses, then write the bundle. The offline loader rebuilds `SlotMaterial`
from the bundle, so stages A-E run without PostgreSQL. A working pattern already exists
in the tests: `services/backend/tests/eval_fixtures.py` `context_from_evidence` builds
`SlotMaterial` from raw slot payloads and calls `context.assemble` with no database. The
2026-11-26 endpoint probe used this path, through `context_for_case("oom-positive")`.

### Splits and versioning

- Snapshots are immutable directories: `datasets/<snapshot-id>/{bundles/, labels.json,
  manifest.json}`. New cases go into the next snapshot.
- Each case is assigned deterministically: `sha256(case_id) mod 10 < 7` goes to dev,
  the rest to test. Assignment is decided at generation time and never changes.
- The test split is sealed. No agent reads its bundles or labels during iteration.
  Only `report --split test` at a milestone may touch it.
- Dataset targets: ≥ 80 cases in total, ≥ 8 per fault kind, ≥ 15 negatives and noise
  cases.

## Experiment protocol

### Configurations

| ID | Configuration |
| --- | --- |
| B0 | Current keyword rules (`evaluation/rules.py`) |
| B1 | Current LLM path (`e06` context v2, prompt v3) over the same bundles |
| B2 | Detector only (A+B) |
| B3 | LLM with compact selection, no signals (C+D) |
| B4 | Full pipeline (A-E) |

Ablations on B4:

- without the signal table
- without hypotheses
- without relevance selection (fixed caps)
- without runbooks
- budget at 16, 32 and 64 KiB
- self-consistency n=1 against n=3
- prompt versions

### Metrics (per split and per family)

**Detection**
- Family accuracy and macro-F1.
- Confusion matrix.
- False positive rate on negatives.

**Abstention**
- Precision and recall of `insufficient_evidence` against labels.

**Citations**
- Evidence-citation precision: cited aliases inside `relevant_sources`.
- Evidence-citation recall: `required_sources` covered.
- Hallucinated aliases: must be 0.
- Runbook citation, reported separately.

**Plan**
- Candidate correctness.
- Manual-only correctness for Pending and ImagePull.

**Output health**
- Schema-valid rate on the first attempt.
- Repair rate.

**Cost**
- Message bytes (p50, max).
- Overflow count.
- Prompt and completion tokens.
- Latency (p50, p95).

**Statistics**
- Wilson intervals for rates.
- Paired McNemar test between two configurations on the same cases.
- Report n for every number.

### Acceptance rule for keeping a change (dev split)

- Macro-F1 does not decrease, and the targeted metric improves.
- Negatives false positive rate stays 0. Hallucinated aliases stay 0.
- No family loses more than one case relative to the current best.
- Change one stage per experiment.

### Failure taxonomy

Assign one tag per failed case. Tagging is automatic where possible.

| Tag | Automatic test | Lever |
| --- | --- | --- |
| `CONTEXT_OVERFLOW` | `budget_exceeded` | C encoding or selection |
| `SIGNAL_DROPPED` | A label `required_sources` entry exists in the bundle but was not sent | C relevance |
| `MISSING_SOURCE` | A required slot is unavailable in the bundle | Collector or lab |
| `WRONG_FAMILY` | Category mismatch with the source present | A, B or D |
| `BAD_CITATION` | Category passes, evidence citation fails | D prompt, E validator |
| `ABSTAIN_WRONG` | Abstention mismatch | B threshold, D prompt |
| `SCHEMA_FAIL` | Parse or structural failure after repair | D prompt, E |
| `SCORER_SUSPECT` | Manual review shows the label or scorer is wrong | Fix the label in a new snapshot, never in place |

