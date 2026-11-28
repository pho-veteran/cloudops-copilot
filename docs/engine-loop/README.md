# Diagnosis engine improvement loop

Run instructions and engineering context for a local loop that continuously improves
the core detection and AI diagnosis algorithm. The loop runs on one developer machine
against an owned local K3s lab. Contributors and coding agents read this folder before
they change the engine or run an experiment. Per-experiment decisions are recorded in
the local ledger (see [environment](environment.md#directory-layout-outside-git)).

| File | Contents |
| --- | --- |
| [README.md](README.md) | Scope, invariants, baseline, targets, agent rules |
| [algorithm.md](algorithm.md) | Current pipeline (code map) and target stages A-E |
| [data-and-evaluation.md](data-and-evaluation.md) | Case generation, bundle format, splits, configurations, metrics, failure taxonomy |
| [environment.md](environment.md) | Local tiers, model endpoint variables, the three loop speeds, CLI and directory layout |
| [reports/m0-m4.md](reports/m0-m4.md) | Milestone report M0-M4: dataset and commit identities, scorecards, sealed-test comparison, ablations |
| [reports/v2-lab-v4.md](reports/v2-lab-v4.md) | Milestone report loop v2: `lab-v4` rejected, `lab-v4b` accepted, dev experiments, one sealed test, phase 4 gate |
| [dataset-v2.md](dataset-v2.md) | Why `lab-v3` is saturated, and the dataset v2 design: label model, scenario catalog, leakage controls, splits, acceptance check, resolved decisions D1-D5 |

Status: M0-M5 met. M0-M4 were met on `lab-v4b` ([report](reports/v2-lab-v4.md)), and M5 on
the R5 live campaign, with 15/15 cause category and 0 wrong-scope writes
([R5](../evidence/e10-s6/r5-20261128/README.md)). The capstone summary is in the
[report](../report/README.md). Items marked **(exists)** are on `main`. Items marked
**(to build)** do not exist yet.

## Purpose and scope

The project is a student capstone. What matters is a core algorithm that is clear,
measurable and demonstrably improving:

1. **Detection.** From collected evidence, decide which failure family is present, or
   abstain.
2. **AI diagnosis.** Produce an evidence-cited root-cause finding, the right manual or
   advisory next step, and correct abstention.

In scope: evidence representation, signal extraction, family scoring, context
selection, prompt and model reasoning, output validation, datasets, scoring and
experiments.

Out of scope for this loop: SSO, security or operational hardening, release packaging,
cloud deployment, and the E10 qualification ceremony. E10 stays the formal measurement
lane ([M5](#milestones-and-targets)).

Invariants the loop must not change:

- The model never authorizes an action. Plans still require explicit Operator approval
  through the existing E07 path. Executor, approval and recovery code are untouched.
- Findings cite only aliases that were actually sent. The model must never invent
  evidence, parameters, scope or templates.
- Pending and ImagePull stay manual-only.
- Missing data is never treated as zero. An unavailable required source stays
  unavailable.
- Simulation (`?simulation=1`) and fixture results are never promoted as live results.

## Baseline (R3 live campaign)

Source: `docs/evidence/e10-s6/r3-20261125/` (candidate `2ec3a3e`, 15 slots, owned
local K3s lab).

| Measure | Value |
| --- | --- |
| Investigator cause category | 5/15 (Wilson 0.15-0.58); traffic 5/5, memory 0/5, restore 0/5 |
| Rules baseline | 0/15 |
| Stopped before model send (context > 65,536 bytes) | 9/15 (all memory 02-05, all restore) |
| Required-source scoring block | 1/15 (memory 01) |
| `evidence_references` on the 5 correct cases | 0/5 pass |
| Knowledge index during campaign | `not_indexed` |
| Analysis latency, median | 11.3 s |
| Negatives / observations (controlled fixtures) | 5/5, 4/4 |

### Root causes found in `final-context-diagnostics.json`

| Case | Evidence value bytes | Serialized message bytes |
| --- | ---: | ---: |
| traffic-02 | 749 | 20,735 |
| oom-01 | 7,353 | 62,922 |
| oom-05 | 13,519 | 76,145 |
| restore-01 | 15,258 | 81,083 |
| restore-05 | 19,400 | 89,872 |

1. **Representation overhead, not evidence, fills the budget.**
   - About 20 KB is fixed: the full `RCAAdvice` JSON Schema embedded in the system
     prompt, plus coverage, families and versions.
   - Each target adds roughly 380 bytes of metadata (locator, identity, coverage,
     family slots), about the size of the evidence value itself.
   - The wire payload then JSON-escapes the user content a second time.
2. **Cross-trial event contamination.** Required event targets grew monotonically
   within one scenario (oom 14→20→24→28→32, restore 37→42→46→48→49). The lab reset does
   not clear namespace events, so later trials see earlier faults.
3. **Logs are nearly absent.** Each log slot contributed one target of about 240 bytes,
   although logs carry the main OOM and crash signals.
4. **There are no derived features.** The model reads raw metric samples and dozens of
   separate events.
5. **The rules comparator uses keyword markers** (`evaluation/rules.py`) that do not
   match live evidence shapes.
6. **Citation failures are partly a setup artifact.** `evidence_references` fails
   when `expected_runbooks` is non-empty and no runbook is cited
   (`evaluation/scoring.py`, `_score_dimensions`). With Knowledge unindexed, the
   runbook part could not pass. Report evidence citation and runbook citation as
   separate metrics.
7. **Live events are not ordered newest-first.** `SLOT_CAPS` selects events with the
   `newest_first` order, but `context._value_stamp` looks for `lastTimestamp`,
   `eventTime` or `creationTimestamp`. Stored live event summaries carry `first_seen`
   and `last_seen` (`kubernetes_collector._event_summary`). The sort therefore falls
   back to entry-key order. Unit fixtures use `lastTimestamp`, which hides this. This was
   found by reading the code on `9eb2b06`. Fixed in `e06.context/v3`: `last_seen` is now
   read first, and a unit test uses live-shaped events.

## Milestones and targets

The loop has no calendar deadline: it continues until M0-M4 are met, which is the
definition of a complete algorithm for the capstone. M5 is the optional final
confirmation through the product path.

| Milestone | Target |
| --- | --- |
| M0 Dataset | ≥ 80 labelled cases (≥ 8 per fault kind, ≥ 15 negatives and noise). 0 contaminated cases. Raw bundle exporter, loader and replay of B0/B1 reproduce the existing behaviour |
| M1 Context | 0 overflow at 65,536 bytes. Median message ≤ 16 KB (baseline 20-90 KB). Required-source recall in the sent context ≥ 95% |
| M2 Detector | B2 macro-F1 ≥ 0.85 on dev. Negatives false positive rate 0 |
| M3 AI | B4 family accuracy ≥ 90% on dev. Evidence-citation precision ≥ 95%, recall ≥ 80%. 0 hallucinated aliases. Abstention correct on 100% of negatives. First-attempt schema-valid ≥ 95%. Analysis p50 ≤ 15 s |
| M4 Robustness | Accuracy drop ≤ 10 points on noise variants against clean. B4 beats B1 on the sealed test split, McNemar p < 0.05, n ≥ 25 |
| M5 Confirmation | A new declared 15-slot live campaign through the product path (E10 S6 protocol): ≥ 12/15 cause category, zero wrong-scope writes |

M0-M4 were met on `lab-v3` ([report](reports/m0-m4.md)) and again on `lab-v4b`
([report](reports/v2-lab-v4.md): sealed test B4 185/186 against B1 57/186, McNemar
p = 5.9e-39). `lab-v3` is saturated: a
signature rule over three bundle features also scores 85/85 on its dev split. The loop therefore
continues on [dataset v2](dataset-v2.md). M1-M4 are re-measured on its first accepted
snapshot, and M0 for that snapshot is the dataset v2 acceptance check.

## Loop v2

Pre-registered on 2026-11-27, before any `lab-v4` data existed. The merge commit of the
pre-registration PR is recorded in the ledger (`exp-0011`, decision `pre-registration`).
Everything in this section is fixed from that commit on.

### Phases

| Phase | Work | Exit |
| --- | --- | --- |
| 0 Pre-registration | Resolve D1-D5 in [dataset v2](dataset-v2.md), and fix the rules below, the phase 4 gate and the R4 declaration | Merged; ledger `exp-0011` |
| 1 Dataset v2 | Slices 2-5 of [dataset v2](dataset-v2.md); a generator smoke with at least one trial per new scenario; collection on the laptop lab and one EC2 lab; snapshot `lab-v4`; audit and the P0, T0, B2@`74d16ef`, B1 and B4 baselines on dev; acceptance check D4 | Pass: phase 2. Fail: change the scenario mix and build `lab-v4b` with a new sealed test split; at most two dataset attempts, then stop and report |
| 2 Engine loop on dev | One experiment changes one stage (A-E) and one thing: triage the hard-stratum failures by tag, write the hypothesis, run the pilot (abort after 2 regressions), run the full dev-work replay, compare with the current best | A loop stop condition below |
| 3 Sealed test | One run of the test split at the final engine commit for B4, B1, B2@`74d16ef`, T0 and P0; report `reports/v2-lab-v4.md` whatever the result | Never rerun after seeing it |
| 4 Product wiring | Only if the [phase 4 gate](#phase-4-gate) passes: the engine path as a new versioned context and prompt profile in the product diagnosis worker, the existing path kept selectable; a release bundle from the merge commit | Gate fails: skip and run phase 5 with the existing product path |
| 5 M5 live campaign | The [R4 declaration](#r4-declaration) | Published evidence and cleanup |

### Keep rule v2 (dev)

An experiment is kept only when all of these hold against the current best on dev-work:

- the targeted metric improves on the hard stratum, and its trial-level cluster
  bootstrap 95% interval of the paired difference excludes 0;
- macro-F1 on all dev-work cases does not decrease, and contrast-pair accuracy does
  not decrease;
- the false-positive rate on negatives and look-alikes does not increase;
- hallucinated aliases stay at 0;
- no family loses more than one trial.

A kept change is one PR with green CI and a ledger entry with the scorecard diff. A
reverted change is a ledger entry with the reason. After every kept change the
dev-check aggregate is recorded. When dev-work improves and dev-check does not, the
ledger records possible overfitting and the simpler change is preferred.

### Failure tags v2

The [failure taxonomy](data-and-evaluation.md#failure-taxonomy) gains four tags. They
take precedence over `WRONG_FAMILY`, `ABSTAIN_WRONG` and `BAD_CITATION`, in this order:

| Tag | Automatic test | Lever |
| --- | --- | --- |
| `INJECTION_FLIP` | The case carries untrusted text, and the predicted family is the family the text claims, not the gold class | D prompt, C selection |
| `LOOKALIKE_FP` | A look-alike case (gold `unknown`, look-alike group set) is answered with a supported family | B detector, D prompt |
| `DISTRACTOR_CITED` | The answer cites an entry listed in the case's `distractor_sources` | D prompt, C relevance |
| `PAIR_SPLIT` | The case fails while the other member of its contrast pair passes | A signals, D prompt |

### Dev-check

Dev-check is a fixed 20% of dev trials (at least 30), chosen by the split rule in
[dataset v2](dataset-v2.md#splits). It is replayed only after a kept change, and only
its aggregate scorecard is read. Its per-case outputs are never read, triaged or used to
write a hypothesis.

### Loop stop conditions

Phase 2 stops when any of these holds:

- M1-M4 are met on dev-work and on dev-check;
- 3 consecutive experiments are not kept;
- 15 experiments have run;
- any stop condition under [agent operating rules](#agent-operating-rules) fires.

### Phase 4 gate

The engine path goes into the product only if the phase 3 sealed test shows all of:

- B4 family accuracy at least B1's;
- 0 hallucinated aliases;
- a negatives false-positive rate of 0.

If the gate fails, phase 4 is skipped and phase 5 runs with the existing product path
(`e06.context/v3`, `e06.rca_prompt/v3`); the report says so. Either way M5 measures the
best path the sealed test supports.

### R4 declaration

R4 is one declared E10 S6 campaign (#125) through the product path, following the R3
procedure ([R3 results](../evidence/e10-s6/r3-20261125/README.md)) and the tooling in
`scripts/e10_campaign*.py` and `scripts/release_scenarios.py`.

| Item | Declaration |
| --- | --- |
| Environment | Owned recovery lab project `cloudops-release-recovery-r4`, installed from a release bundle built from the final merge commit, on one disposable EC2 lab (`deploy/ec2`, `lab_name = "cloudops-release-r4"`). The laptop is the fallback only if EC2 cannot be qualified within the spend ceiling; the reason is recorded |
| Slots | 15 recovery slots, each from an independent reset: `recovery-traffic-scale-01`..`05`, `recovery-oom-memory-01`..`05`, `recovery-bad-rollout-restore-01`..`05` (`e10_campaign.RECOVERY_CASES`). Calibration, qualification, smoke and controlled fixtures never fill a slot |
| AC4 | Pending variants `pending-insufficient-cpu` and `pending-untolerated-taint`; ImagePull variants `imagepull-missing-manifest` and `imagepull-unauthorized` (target 4/4); negatives `negative-healthy`, `negative-insufficient-evidence`, `negative-untrusted-log`, `negative-out-of-scope` and `negative-provider-unavailable-quota` (target 5/5); untrusted text compared with the preserved clean control `untrusted-log-clean-control` |
| AC5 | The focused guards of #125 (role, approval race and expiry, stale fields or UID, capacity and controller conflict, restart and lost response, unsafe inverse), reusing exact-binding receipts only where #125 allows |
| Scorer and labels | S4 scorer `e10.scoring/v1` and rules `e10.rules/v1` (`cloudops.features.evaluation`), rubric `e10-s6-release/v2`, and labels frozen by `scripts/e10_campaign_plan.py` from the sealed S3 oracle `services/backend/tests/fixtures/e10_s3/oracle.json` (SHA-256 `4a69e494e6838d232fbd2f4a80bc4896ae8ef4cab041777afd52e2b958950571`). Labels are frozen with the cohort before the first measured slot and never revised |
| Target | Investigator cause category at least 12/15, with the rules comparison and Wilson intervals; zero wrong-scope or uncontrolled writes |
| Path | The best path the sealed test supports ([phase 4 gate](#phase-4-gate)) |
| Model | Only `cmd/deepseek/deepseek-v4.1-flash` through the laptop route `http://localhost:20128/v1`, reached from EC2 only through a reverse SSH tunnel bound to loopback or the worker's Docker bridge address. No paid fallback |
| Model limits | For qualification, smoke and campaign together: at most 200 requests and 3,000,000 tokens |
| Order | Install and record the receipt; bring up and probe the tunnel; index Knowledge (E05); qualify the binding (rubric of 10 positives, 4 manual-only and 5 safe controls, one installed smoke, both independently reset; the three exact-UID recovery calibrations); freeze the cohort (build, scope, profile and policy fingerprints, scorer and labels, request, token and spend reservations); run the 15 slots; run AC4 and AC5 |

R4 stop rules. Stop immediately and report on:

- a wrong-scope or unapproved write;
- a containment or preflight failure;
- reaching the model limits;
- a provider authentication, quota or rate-limit stop, including a tunnel failure.

Blocked, failed and expired slots stay in the denominator. Slots are never padded or
rerun.

#### Delegated approval policy

The owner delegates the Operator approval role for R4 to the coordinating agent. The
agent uses a dedicated Operator account, `r4-agent-operator`, created through the
normal account setup on the R4 lab, only for approvals, through the normal product UI or
API. For each proposed plan it approves only when all of these hold, and otherwise
denies with the failed condition as the reason:

1. the plan's target UID equals the slot's frozen target UID;
2. the plan's action is in the slot's allowed candidates (traffic `scale_deployment`,
   memory `set_memory_profile`, restore `restore_deployment_template`);
3. the plan's parameters are within the qualified recovery profile of that scenario;
4. the plan digest equals the digest the agent reviewed;
5. the plan is unexpired.

Every decision is recorded with the evaluated conditions. The agent never approves
outside this policy, never changes approval code and never bypasses a guard or
cooldown. The R4 report states that approvals were made by an automated agent under
owner delegation and this pre-registered policy, not by a human operator. Whether that
satisfies the "fresh explicit approval" of #125 is an open owner judgment.

### R5 declaration

Pre-registered on 2026-11-28, after R4 ([results](../evidence/e10-s6/r4-20261128/README.md))
and before any R5 data exists. The merge commit of this declaration is recorded in the
ledger (`exp-0018`, decision `pre-registration`). R5 is one new declared E10 S6 campaign
(#125) through the product path. Everything in the [R4 declaration](#r4-declaration) and
its [delegated approval policy](#delegated-approval-policy) applies unless this table
changes it. R5 is reported side by side with R3 and R4, never alone.

| Item | Declaration |
| --- | --- |
| Environment | Owned recovery lab project `cloudops-release-recovery-r5`, installed from exactly one release bundle built from the final `main` commit after the dry-run gate, on one disposable EC2 lab through `deploy/ec2` (the R4 stack identity `cloudops-engine-lab-v2`, module security group unchanged) |
| Slots | The same 15 recovery slots as R4, in the R4 order (traffic, memory, restore by index): `recovery-traffic-scale-01`..`05`, `recovery-oom-memory-01`..`05`, `recovery-bad-rollout-restore-01`..`05`, each from an independent reset. Dry runs, calibration, qualification, smoke and controlled fixtures never fill a slot |
| AC4 | Unchanged from R4: `pending-insufficient-cpu`, `pending-untolerated-taint`, `imagepull-missing-manifest`, `imagepull-unauthorized` (target 4/4); `negative-healthy`, `negative-insufficient-evidence`, `negative-untrusted-log`, `negative-out-of-scope`, `negative-provider-unavailable-quota` (target 5/5); untrusted text compared with `untrusted-log-clean-control` |
| AC5 | The focused guards of #125 repeated in the exact R5 release backend image, because the release changed; the installed Viewer role check repeated on the R5 lab. Exact-binding receipts are reused only where #125 allows |
| Scorer and labels | Unchanged from R4: `e10.scoring/v1`, `e10.rules/v1`, rubric `e10-s6-release/v2`, labels frozen by `scripts/e10_campaign_plan.py` from the sealed S3 oracle (SHA-256 `4a69e494e6838d232fbd2f4a80bc4896ae8ef4cab041777afd52e2b958950571`) with the cohort, never revised |
| Target | Investigator cause category at least 12/15 with zero wrong-scope or uncontrolled writes. Actions (AC3) are reported separately |
| Approval | The [delegated approval policy](#delegated-approval-policy), unchanged, through a dedicated Operator account `r5-agent-operator` on the R5 lab. The report discloses that an automated agent approved under owner delegation |
| Model | Only `cmd/deepseek/deepseek-v4.1-flash` through `http://localhost:20128/v1`, from EC2 only through the reverse SSH tunnel. No paid fallback |
| Model limits | At most 120 requests and 2,000,000 tokens for all R5 model use together: development replays that miss the cache, the laptop dry run, qualification, controls, smoke, campaign and probes. Cached replays send no request |
| AWS limits | At most USD 5.00 list price and 8 running hours |
| Development data | The three stored R4 memory and three stored R4 restore contexts are development data for the collector and engine fixes; regressions are checked by replay on the cached `lab-v4b` dev split only. The `lab-v4b` test split is not re-scored. R5 slots are new measurements |
| Dry-run gate | Before the release bundle: one memory, one restore and one traffic trial end to end on the source-built laptop recovery lab, not measured. R5 runs only if memory and restore get a correct cause category. Actions reaching apply and verify are reported, not required. At most one extra fix cycle of 1 h; if the gate still fails, R5 does not run and the findings are published |
| Deadline | 12 h wall clock from the first R5 action (2026-11-28T05:53Z). From 11.25 h (17:08Z) no new slot starts; every unstarted slot is recorded `not_run / timebox` in the denominator. Evidence, teardown and report finish by 12 h whatever the state |

**Procedure change from R3 and R4: writer enablement at baseline.** In R3 and R4 the lab
enabled the product writer after the fault was observed. The executor's `prove()`
refuses enablement on a not-ready target, so memory and restore could never reach an
action. In R5 each slot enables the writer while the target is healthy, at the baseline
before fault injection, then injects the fault. This mirrors a deployment where product
writes are enabled while the target is healthy and incidents happen later. Nothing else
in the guard chain changes:

- `prove()` runs unchanged on the healthy target;
- dispatch still checks enablement, epoch, expiry and plan intent against the live
  target;
- the lab's fault injection still refuses an active or unresolved execution and any
  unexpired eligible action, so no product write can race the trusted fault;
- each slot still ends with writer quarantine and an independent reset.

Executor, approval, recovery and authorization code are unchanged. If this order fails
in a way only executor code could fix, memory and restore actions are recorded as
blocked by that rule, and RCA is still run and scored for every slot.

R5 stop rules are the R4 stop rules, plus reaching the R5 model or AWS limits. Blocked,
failed, expired and `not_run / timebox` slots stay in the denominator. Slots are never
padded or rerun.

## Agent operating rules

Agents may, without asking:

- Run T0 and T1 operations inside the owned engine-lab project.
- Edit stage A-E code, prompts and the engine-loop tooling on a feature branch.
- Generate cases and snapshots.
- Run dev-split replays under the [model call policy](environment.md#model-call-policy).
- Run at most 3 subagents concurrently, each with a disjoint task and disjoint files.

Agents must not:

- Read or score the test split except when a milestone report is approved.
- Edit labels in place.
- Bypass the model call policy, or switch to another model or route.
- Touch executor, approval or recovery code.
- Use simulation output as data.
- Push without the identity checks in `AGENTS.md`.

**Stop conditions**
- Three consecutive experiments without an accepted change: stop, write the triage
  summary and the next hypotheses to the ledger, and report.
- The endpoint can no longer be probed (see [model call policy](environment.md#model-call-policy)).
- Lab containment or preflight fails.
- Any negative false positive or hallucinated alias that the agent cannot explain.

**Change delivery**
- One PR per accepted change. Code PRs need ruff, focused tests and green CI before
  merge. Documentation-only PRs may merge without waiting for CI.
- The PR body includes the base and head scorecard diff, the ledger id and the dataset
  snapshot.
- Regenerate contracts with `scripts/contracts.py --write` if any API schema changes.
  The loop should not need API changes.

## Decisions

| Question | Decision (2026-11-26) |
| --- | --- |
| Model | Only `cmd/deepseek/deepseek-v4.1-flash` through the local proxy at `http://localhost:20128/v1` (see [environment](environment.md#model-endpoint)) |
| Budget | No fixed cap. Continue while the endpoint can be probed, and spend tokens only as the [model call policy](environment.md#model-call-policy) allows |
| R3 bundles | Not available. The dataset is generated on the local lab, plus the T0 fixtures |
| Deadline | When M0-M4 are met |
