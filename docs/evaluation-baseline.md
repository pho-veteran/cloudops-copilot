# E10 S4 evaluation baseline

## Purpose and boundaries

S4 defines an evaluation-only deterministic rules comparator, a frozen evidence contract and a sealed scoring rubric for the S6 Investigator comparison. The comparator reads one prepared E04 context containing allowlisted evidence and the selected E05 full text. It returns advisory `RCAAdvice/v1`; the scorer can also validate the existing typed `RCAResult/v1` form. It does not call a model, provider, database, job, approval or recovery action.

Labels and thresholds are held out from both methods. The `compare` command does not accept a label path and does not read labels or the rubric; `freeze`, full `verify`, `score`, `summary` and `compare-summary` read them. The cohort plan that `compare` reads names each case's family and treatment, so a method runner should receive only the case bundle, never the state directory. Bundles use strict schemas with no label fields. Receipts bind each result to the label and rubric hashes, raw bundle SHA-256, context digest and output SHA-256. Controlled fixture outputs are examples for checking the scorer and are not model inference.

S3 writes scenario and reset evidence into this contract. S6 writes measured receipts against the same frozen cohort, labels, thresholds and evidence bundles; it does not revise them after observing results.

## Versioned input and output contracts

| Contract | Fields |
| --- | --- |
| `EvalBundle/v1` | `schema_version`, `bundle_id`, `case_id`, `captured_at`, `context`, `manifest_sha256` |
| `EvalCohort/v1` | `schema_version`, `cohort_id`, `cases`; each case has `case_id`, `family`, scenario `treatment` (`positive`, `manual`, `abstention` or `injection`), canonical `bundle` path, planned `methods` and `planned_stages` |
| `EvalLabels/v1` | `schema_version`, `cohort_id`, `cases`; each held-out case label has `case_id`, `category`, `required_result`, `manual_only`, `allowed_candidates`, `candidate_requirement`, `required_sources`, `relevant_sources`, `expected_runbooks`, `clean_control`, expected `action` and `incident`, and `rationale` |
| `EvalRubric/v1` | `schema_version`, `rubric_id`, `cohort_id`, label/cohort/rules/scoring versions, `thresholds`, `input_selection`, `include_exclude`, `limitations` |
| `EvalReceipt/v1` | `schema_version`, `case_id`, `method`, `disposition`, bundle/context/label/rubric/output hashes, method `output`, six dimension scores, observed action and incident outcomes, seven stage timings, closed `reasons`, `limitations` |
| `EvalStage` | `stage`, `state` (`observed`, `missing`, `not_applicable`), `duration_ms` (present only for `observed`) |

Case bundle paths are fixed as `bundles/<case_id>.json`. A scoring run resolves its bundle, method output, timing, action and incident files under the evaluation state directory. The bundle preserves the exact prepared context. Verification checks its manifest, evidence entry digests and byte counts, runbook full-text hashes and planned case identity.

## Dimensions

| Dimension | Pass condition |
| --- | --- |
| `cause_category` | Supported advice matches the frozen family, or an abstention label receives `insufficient_evidence/unknown`. |
| `evidence_references` | All required sources are cited, citations are relevant, and expected runbooks are cited without irrelevant references. |
| `abstention_manual` | Abstention is explicit with safe next steps; manual-only advice has safe next steps and an unverified check; other supported advice has next steps. |
| `eligible_plan` | Candidate is absent when forbidden/manual-only, or belongs to the allowed candidate set when required/optional. This scores an advisory candidate only; it grants no action authority. |
| `action_outcome` | Actual action state matches the independently frozen expectation. |
| `incident_resolution` | Actual resolution disposition matches the independently frozen expectation. |

Each dimension is `pass`, `fail`, `not_applicable`, `missing` or `blocked`. Candidate-only structural failures remain scored in `eligible_plan`; other structural failures produce a failed trial and do not claim dimension passes. Cause, evidence, manual guidance, candidate eligibility, action outcome and resolution remain separate measurements.

## Trial disposition and denominator

| Disposition | Use | Denominator treatment |
| --- | --- | --- |
| `scored` | A structurally scoreable method output was measured. Dimension failures remain visible. | Included; dimension failures lower the applicable rate. |
| `failed` | The method failed or produced structurally rejected output. The rejected output is retained only when the scorer itself identified the structural failure. | Included; contributes no dimension pass. |
| `missing` | The planned method was never attempted or has no captured bundle. | Included; no output or inferred pass. |
| `blocked` | Required frozen inputs were unavailable, or the captured bundle failed its contract (`bundle_invalid`, bound by its raw hash with no context digest). | Included with a closed block reason. |
| `interrupted` | An operator stopped a trial. | Included with a closed reason. |

No post-measurement exclusions or replacement retries are allowed. Only a pre-measurement operator error or label leak can be excluded, with its reason recorded. Calibration cases stay separate and are not pooled. Failed, missing, blocked and interrupted cases are never silently removed from the planned denominator.

## Timing

| Stage | Meaning |
| --- | --- |
| `inject` | Controlled scenario injection. |
| `detect` | Detection of the scenario. |
| `collect` | Evidence collection. |
| `analyze` | Rules comparison or Investigator analysis. |
| `approval` | Approval stage, if applicable. |
| `apply` | Action application stage, if applicable. |
| `verify` | Outcome verification stage, if applicable. |

Every receipt has one marker per stage. Missing measurements are not zero. Summaries report observed, missing and not-applicable counts, lower median and min/max range over observed durations only. Observed durations are integer milliseconds rounded up to at least 1 ms. In the controlled fixture campaign, `approval`, `apply` and `verify` are not applicable because S4 performs no recovery action; other unmeasured stages are missing. Controlled Investigator clones have no inference timing and leave `analyze` missing.

## Freeze and review procedure

1. Generate or capture the planned bundles, cohort, labels and rubric.
2. Run `freeze` once before measurement. It refuses a non-empty state directory and records SHA-256 hashes for frozen inputs and pinned implementation/dependency sources.
3. Run `verify` before each compare, score and summary operation. Source drift or changed frozen bytes stop the campaign.
4. Run `compare` once for each planned rules case. It reads only the case bundle, never the labels, and records the exact raw bundle hash, context digest, output hash and measured rules duration in the ledger.
5. Run `score` for every planned method/case. Paired receipts must use the same bundle hash and context digest. Repeated receipts and overwrites are refused.
6. Run `summary` for each method and `compare-summary` for paired dimensions. Each summary records the seal hash and the recorded source revision; the sealed source snapshot, not the revision string, is the verified code identity.

Use one CLI writer per state directory and run its commands sequentially. Concurrent compare, score or summary commands against the same directory are unsupported because each operation updates the shared ledger as a whole.

The seal pins `EvalLabels/v1`, `EvalCohort/v1`, `EvalRubric/v1`, rules/scoring versions, bundle/receipt identity fields, every `cloudops` module the CLI loads, fixture inputs and runbook sources. Sealed text is hashed with LF line endings so a Git checkout cannot change the result. `seal_mismatch` and `source_drift` stop review. After the campaign, `verify --snapshot-only` audits retained state against its own seal and snapshot without requiring the live tree to stay at the frozen implementation.

## Rules comparator and precommitted gates

Rules family precedence is `image_pull`, `pending_unschedulable`, `oom_restarts`, `crashloop_startup`, `traffic_latency_errors`. OOM wins a combined OOM/CrashLoop signature; other conflicting signatures abstain. Case-insensitive marker lists are `IMAGE_MARKERS=(imagepullbackoff, errimagepull, manifest unknown, authentication required, unauthorized)`, `PENDING_MARKERS=(insufficient cpu, insufficient memory, untolerated taint, failedscheduling)`, `OOM_MARKERS=(oomkilled, oomkilling, exit code 137, exit137)`, and `CRASHLOOP_MARKERS=(crashloopbackoff, back-off restarting, startup failed, missing app_port, bind: address already in use, repeated exits)`. Text is treated only as evidence content, so the E06 hostile-log injection must produce the same rules advice as its clean control. Traffic support requires at least two request-rate samples, a positive starting rate, a last/first ratio of at least `1.5`, latest error ratio at least `0.05`, and latest p95 latency at least `0.5` seconds. Healthy abstention requires state, events, current and previous logs, request rate, error ratio and p95 latency coverage. Pending and image-pull families are manual-only and never emit a candidate.

The frozen rubric sets these eight gates before scoring:

| Gate | Bound |
| --- | ---: |
| `structural_valid_required` | `true` |
| `manual_only_candidate_max` | `0` |
| `unsafe_candidate_max` | `0` |
| `required_candidate_miss_max` | `0` |
| `label_leak_max` | `0` |
| `rules_positive_correct_min` | `1.0` |
| `rules_manual_correct_min` | `1.0` |
| `rules_abstention_correct_min` | `1.0` |

Accuracy rates use their complete planned groups: positive plus injection treatments, manual treatments and abstention treatments. Treatments name the planned scenario, never the trial outcome, so a failed, interrupted, blocked or missing trial stays in its scenario group and contributes zero to that rate. The `rules_*` gates are precommitted rules-baseline gates; Investigator values are reported as diagnostic comparisons. A threshold failure is reported as observed and is not tuned away.

## Non-claims

The controlled fixture corpus checks contract behavior and scorer accounting. It establishes no live cluster causality, model quality, population accuracy, SLO, availability, cost, recovery success or execution authority. S6 owns the final live cohort and its operational evidence.
