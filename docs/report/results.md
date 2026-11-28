# Results

Part of the [capstone report](README.md). The tables restate published results. Each
section names its source, and the source is authoritative.

## 1. Starting point: live campaign R3

R3 scored the Investigator 5/15 on cause category: traffic 5/5, memory 0/5, restore 0/5.
The rules baseline scored 0/15. Nine slots stopped before the model call, because the
required context exceeded 65,536 bytes.

Reading `final-context-diagnostics.json` gave the first engineering targets
([analysis](../engine-loop/README.md#baseline-r3-live-campaign)):
- representation overhead filled the budget;
- events leaked between trials;
- logs were nearly absent from the context;
- live events were not ordered newest-first.

Source: [R3 evidence](../evidence/e10-s6/r3-20261125/README.md).

## 2. Engine loop v1 on `lab-v3`

The v1 loop added these stages:
- **A.** Signal extraction.
- **B.** An explainable weighted detector.
- **C.** Compact, budget-aware context selection.
- **D.** A hypothesis-confirmation prompt.

Six PRs were merged: #150 to #155.

| Split | B1 | B4 |
| --- | --- | --- |
| Dev (85 cases) | 53/85 | 85/85 |
| Sealed test (35 cases) | 21/35 | 35/35 (McNemar p = 0.000122) |

**The dataset was saturated.** Every B4 ablation also scored 85/85. The dataset audit
added later (PR #156) showed that a signature rule over three bundle features scored
85/85 on dev: Warning event reasons, an `OOMKilled` termination, and the maximum error
ratio or p95. Each family had one Warning-reason signature, and every injection caused
a rollout that no healthy negative had. `lab-v3` could not rank engine versions.

Sources: [M0-M4 report](../engine-loop/reports/m0-m4.md) and
[dataset v2 design](../engine-loop/dataset-v2.md#why-lab-v3-cannot-drive-the-loop).

## 3. Engine loop v2 on `lab-v4b`

The design and its thresholds were pre-registered before any data existed: PR #157, ledger
`exp-0011`. The dataset acceptance check (D4) requires 0 leaks, T0 ≤ 0.60 on dev, and at
least 40% hard trials.

| Dataset attempt | Trials | Outcome |
| --- | --- | --- |
| `lab-v4` | 71 of 334 planned | Rejected: dev T0 0.84, 16% hard. Stopped early; its test trials were never read |
| `lab-v4b` | 338 of 338 | Accepted: 0 leaks, dev T0 0.454, 54.6% hard. Dev 152 trials (31 in dev-check), test 186 |

The trials were collected on two hosts, the laptop and EC2. 131 trials were collected again
with the same seeds after the lab reader tokens expired.

**Dev experiments** (121 dev-work trials, keep rule v2):
- `exp-0014`, kept. Stage E `required_citations` moved unsupported aliases out of the
  answer. Hard-stratum pass rate rose from 56/67 to 67/67, and dev-check pass rate from
  0.871 to 1.0.
- `exp-0015`, reverted. Low reasoning effort cut latency, but its bootstrap CI included 0.

Sealed test, scored once at `e906088`:

| Measure | Result |
| --- | --- |
| Family accuracy | B4 185/186 · B2 183/186 · B1 57/186 · T0 0.489 · P0 0.296 |
| Paired tests | B4 vs B1 p = 5.9e-39 · B4 vs B2 p = 0.625 · B4 vs T0 p = 2.5e-27 |
| Hard stratum, held-out strata | 95/95 hard · `kv` 24/24 · `mid` 8/8 |
| Safety of the answer | 0 hallucinated aliases · negatives FP 0/55 · look-alike FP 0/35 · distractor citations 0/696 · untrusted-text flips 0/27 |
| Pairs and hosts | Contrast pairs 24/24 · EC2 112/112 · laptop 73/74 |

Source: [loop v2 report](../engine-loop/reports/v2-lab-v4.md).

## 4. Live campaigns R4 and R5

| Measure | R3 | R4 | R5 |
| --- | --- | --- | --- |
| Environment | Local Docker/WSL | EC2 `c5a.2xlarge` | EC2 `c5a.2xlarge` |
| Writer enablement | After the fault | After the fault | At the healthy baseline |
| Investigator cause category | 5/15 | 5/15 | **15/15** (Wilson 0.80-1.00) |
| Traffic / memory / restore | 5 / 0 / 0 | 5 / 0 / 0 | 5 / 5 / 5 |
| Rules baseline | 0/15 | 0/15 | 10/15 |
| Actions applied and verified | 1 | 5 | 15 |
| Runbook guidance in context | Not indexed | `no_matches` | Matched in every context |
| AC4 observations / negatives | 4/4, 5/5 | 3/4, 5/5 | 4/4, 5/5 |
| Wrong-scope / uncontrolled writes | 0 / 0 | 0 / 0 | 0 / 0 (60 audited writes) |

**Why R4 failed on memory and restore:**
- The memory contexts had no previous container log, so the engine correctly abstained.
- The restore contexts abstained.
- The executor refuses to enable the writer on a not-ready target, and the lab enabled
  the writer after the fault.

**What changed for R5.** Executor, approval and recovery code were not changed.
- **#172:** the collector waits a bounded time for a restarted instance before reading its previous log.
- **#173:** an empty current log of a restarted container is waived, and the runbook query is targeted.
- **#174:** the lab enables the writer at the healthy baseline, before fault injection.

**Engine against rules in R5.** The pairs were: both correct 10, Investigator only 5 (all
traffic), rules only 0. With 15 slots that difference is not significant (exact McNemar
p = 0.0625). The live campaign confirms the product path end to end. The offline sealed
test is where engine versions were compared.

R5 stage medians (AC6):

| Stage | Median |
| --- | --- |
| Detect | 1.3 s |
| Collect | 9.7 s |
| Analyze | 15.0 s |
| Approval | 0.8 s |
| Apply | 7.2 s |
| Verify | 300.4 s |

Verify uses the qualified 180 s window for traffic and 300 s for memory and restore.

Sources: [R4](../evidence/e10-s6/r4-20261128/README.md) and
[R5](../evidence/e10-s6/r5-20261128/README.md).

## 5. Safety

- **Writes.** No campaign made a wrong-scope or uncontrolled write. R5 audited 60
  Deployment writes; every product write tested the frozen target UID and fell inside
  its slot's approved recovery step.
- **Guards.** 91 focused guards passed in the R5 release image:
  - role;
  - approval race and expiry;
  - stale fields and UID;
  - capacity and controller conflict;
  - restart and lost response;
  - unsafe inverse;
  - quota and provider unavailable.

  Viewer requests to propose, decide and inverse returned 403.
- **The model never authorized an action.** Approvals came from a deterministic,
  pre-registered policy under owner delegation. The policy approves only when target,
  action, parameters and digest all match the plan, and denies otherwise.

## 6. Cost

| Work | Model use | AWS (list price) |
| --- | --- | --- |
| Engine loop v1 (`lab-v3`) | 572 requests, 4.45 M tokens | - |
| Engine loop v2 and R4 | 538 replay calls with 3.48 M tokens, plus 62 R4 requests with 159 k tokens | USD 7.91, 24.8 h |
| R5 | 69 requests, 204 k tokens | USD 1.45, 4.5 h |

The model was `cmd/deepseek/deepseek-v4.1-flash` through a local OpenAI-compatible
route, with no paid fallback.
