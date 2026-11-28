# CloudOps Copilot: capstone report

CloudOps Copilot helps a small operations team handle a Kubernetes incident from alert to
verified recovery. It collects scoped evidence, diagnoses the cause with a cited and
validated finding, proposes a bounded recovery plan, and executes the plan only after
explicit approval. Every write is reconciled against the approved plan.

This report summarizes what was built, how it was measured and what the results show.
Each number links to its source.

| Page | Contents |
| --- | --- |
| [Results](results.md) | Offline evaluation of the diagnosis engine, live recovery campaigns R3-R5, safety, timing and cost |
| [Limitations and disclosures](limitations.md) | What the results do not show, deviations from the original protocol, and open work |
| [Evidence index](evidence-index.md) | Each claim mapped to the file that supports it |
| [System design](../system-design/README.md) | Architecture, domain, data model, interfaces, diagnosis and recovery design |
| [Engine improvement loop](../engine-loop/README.md) | Algorithm, datasets, protocol and milestone reports |

## Results at a glance

**Diagnosis engine, offline.** Measured on the sealed `lab-v4b` test split: 186 trials,
scored once at the final engine commit
([report](../engine-loop/reports/v2-lab-v4.md)).

| Method | Family accuracy |
| --- | --- |
| P0 majority class | 0.296 |
| T0 signature rule | 0.489 |
| B1 previous product path | 57/186 |
| B2 deterministic detector | 183/186 |
| **B4 engine (detector + cited LLM confirmation)** | **185/186** |

- B4 beats B1 (exact McNemar p = 5.9e-39).
- B4 against B2 is not a significant difference (p = 0.625).
- B4 made 0 hallucinated citations, 0/55 false positives on negatives, and 0/27 family
  flips from untrusted log text.

**Live recovery campaigns.** Each campaign is 15 slots on an owned lab, through the
product path ([R3](../evidence/e10-s6/r3-20261125/README.md),
[R4](../evidence/e10-s6/r4-20261128/README.md),
[R5](../evidence/e10-s6/r5-20261128/README.md)).

| | R3 | R4 | R5 |
| --- | --- | --- | --- |
| Investigator cause category | 5/15 | 5/15 | **15/15** |
| Rules baseline | 0/15 | 0/15 | 10/15 |
| Actions applied and verified | 1/15 | 5/15 | **15/15** |
| Wrong-scope or uncontrolled writes | 0 | 0 | **0** |

R5 meets the M5 target of at least 12/15. Issue #125 was closed after the owner decision
on agent-made approvals. The [limitations](limitations.md) page lists what this does not
establish.

## Method in brief

1. **Find out why the engine failed.** The R3 campaign failed on memory and restore. Its
   context diagnostics showed oversized messages, cross-trial event contamination and
   missing logs.
2. **Improve the engine offline.** Each change was measured against pre-registered
   acceptance rules on lab bundles, with a sealed test split
   ([loop](../engine-loop/README.md)).
3. **Make the dataset hard enough to rank engines.** The first dataset, `lab-v3`, was
   saturated: a signature rule over three bundle features also scored 85/85 on dev. Dataset v2 added
   look-alikes, red herrings, intensity ladders, opaque ids and a shortcut audit
   ([design](../engine-loop/dataset-v2.md)).
4. **Confirm in production-path live campaigns.** Every planned slot counts in the
   denominator: blocked and failed slots included. A measured slot is never rerun.

## Claim levels

| Level | Meaning in this report |
| --- | --- |
| Fixture-tested | Unit and PostgreSQL tests over labelled synthetic data |
| Offline | Replay of collected lab bundles. Dev-lane and non-qualifying |
| Local live | The owned local Docker/K3s lab through the product path |
| EC2 live | A disposable single-host EC2 lab through the product path |

R4 and R5 are EC2 live. They do not complete the EC2 release deployment (S2/S7) or the
second-operator packet (#126).
