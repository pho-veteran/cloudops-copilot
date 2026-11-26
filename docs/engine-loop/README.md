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

Status: in progress. Items marked **(exists)** are on `main`. Items marked
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
