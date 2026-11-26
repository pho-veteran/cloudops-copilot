# Diagnosis engine: current pipeline and target algorithm

Part of the [engine improvement loop](README.md).

## Current pipeline (exists)

| Step | Code | Behaviour |
| --- | --- | --- |
| Collect | `features/investigations/*`, `kubernetes_collector.py`, Prometheus collector | 12 slots: `kubernetes.state`, `.events`, `.logs.current`, `.logs.previous`, `.configmap.metadata`, six `prometheus.*` series. Bounded to 10 Pods, 20 containers, 200 lines or 32 KiB per log |
| Load | `diagnosis/context.py` `load_materials` | Reads Run evidence rows from PostgreSQL into `SlotMaterial` |
| Family matrix | `context.py` `family_matrix`, `versions.py` `FAMILY_REQUIRED` | A family is eligible only if all of its required slots are usable |
| Select | `context.py` `_select`, `versions.py` `SLOT_CAPS` | Fixed per-slot caps and orders. Events are the 50 newest. Logs are the 3 newest per group. State and metrics up to 250 entries, ordered by key |
| Serialize | `context.py` `_user_content`, `_messages` | JSON payload with aliases E001… and K001…; system prompt plus embedded schema |
| Budget | `diagnosis/egress.py` `check_send` | Fails closed with `budget_exceeded`. Default 65,536 bytes; opt-in ceiling 131,072 (`versions.py` `MAX_INPUT_BYTES`) |
| Model | `diagnosis/transport.py` `ModelClient` | One OpenAI-compatible Chat Completions call |
| Validate | `diagnosis/advice.py` `parse`, `validate_structural`, `normalize` | Alias, family and citation rules; one repair with feedback |
| Rules baseline | `evaluation/rules.py` | Keyword markers and metric thresholds with fixed family precedence |
| Score | `evaluation/scoring.py` `score_case` | Six dimensions: `cause_category`, `evidence_references`, `abstention_manual`, `eligible_plan`, `action_outcome`, `incident_resolution` |
| Bundle | `evaluation/bundle.py` `EvalBundle/v1` | Freezes an already **prepared** context. It cannot replay a different selection or encoding |

Prompt and versions: `PROMPT_VERSION = e06.rca_prompt/v3`, `CONTEXT_VERSION =
e06.context/v2`, `VALIDATOR_VERSION = e06.validator/v1`.

## Target algorithm

```text
RawEvidenceBundle
  A. Signal extraction (deterministic)
  B. Family detector (deterministic, explainable)
  C. Budget-aware evidence selection
  D. LLM reasoning over signals + hypotheses + selected evidence + runbooks
  E. Consistency check + existing validator
  -> RCAAdvice/v1 (unchanged output contract)
```

Each stage has its own version string. Each stage can be switched off for ablation.

### A. Signal extraction (exists: `diagnosis/engine/signals.py`, `engine.signals/v1`)

Input is raw slot entries. Output is a `SignalTable` of typed facts, each with the
source aliases that support it.

| Signal | Derivation |
| --- | --- |
| Termination facts | `lastState.terminated.reason`, exit code, restart-count delta per container (OOMKilled, Error, exit 137, 1, ...) |
| Event groups | Group by `(reason, involvedObject kind/name)`: count, first and last timestamp, one representative message |
| Log templates | Mask numbers, IDs and timestamps, cluster lines into templates (Drain-style). Keep counts, first and last line, error-level flag |
| Rollout facts | Revision or image changed in the window, template diff, ReplicaSet age, unavailable replicas |
| Scheduling facts | `FailedScheduling` reasons (insufficient cpu or memory, untolerated taint), pending duration |
| Image facts | `ErrImagePull` / `ImagePullBackOff` reason class (not found, unauthorized) |
| Metric features | For each series: incident-window mean and max against a pre-window baseline, ratio, threshold crossings, change-point time. Also working set as a share of the memory limit |

Rule: every signal keeps the aliases of its supporting entries, so citations stay exact.

### B. Family detector (exists: `diagnosis/engine/detector.py`, `engine.detector/v1`)

- Score each of the five families from signals, using explicit weighted features
  (versioned weights in code, for example `engine.detector/v1`). Output a ranked list of
  `Hypothesis(family, score, supporting_signals, missing_required_slots)`.
- Abstain when the top score is below a threshold, when the top two are within a margin,
  or when the top family is not eligible in the family matrix.
- This stage replaces the keyword rules as the deterministic baseline (B2) and is the
  prior for stage D.
- Weights can be tuned on the dev split only. Simple logistic regression over signal
  features is an allowed variant, provided it keeps the per-feature explanation.
- v1: threshold 3.0, margin 1.0, fixed weights per named feature; replayed as B2 with no
  model call. Neither stage is used by the product diagnosis path.

### C. Budget-aware selection and compact encoding (to build)

- **Compact encoding.** Send only `alias`, `slot` and `value` per target, plus grouped
  signals. Keep locator, identity and digests server-side; citation resolution already
  uses the stored context. Replace the embedded full JSON Schema with a short field
  list, and keep schema enforcement in the validator.
- **Selection.** Required evidence for the top-k hypotheses comes first. Then fill
  greedily by `relevance / bytes`. Relevance comes from stage A and B: supporting
  aliases, error-level log templates, recent events.
- **Never fail silently.** If the budget is still exceeded, drop lowest-relevance entries
  and record each omission in the context (`omitted: {slot: count}`). Fail closed only
  when a required slot would end up with zero entries.
- Target: median message ≤ 16 KB and 0 overflow at 65,536 bytes on all dataset cases.

### D. LLM reasoning (prompt versions `engine.rca_prompt/vN`, to build)

- The input is the request, the signal table, the ranked hypotheses with scores, the
  selected evidence and runbook excerpts.
- The model must confirm or reject each top-k hypothesis with cited aliases, then emit
  `RCAAdvice/v1`.
- Allowed variants: few-shot examples (taken from the dev split only), self-consistency
  (n samples, majority family), temperature, reasoning effort.

### E. Consistency check (to build)

- Run the existing validator unchanged (`advice.validate`).
- When the detector and the LLM disagree, and the LLM's family is outside the
  detector's top-2 with a low score, downgrade to `insufficient_evidence`. Alternatively
  flag the case for review. Which policy applies is an experiment variable.

