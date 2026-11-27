# Milestone report: loop v2 on `lab-v4b`

Part of the [engine improvement loop](../README.md). This report covers loop v2: dataset
v2, the engine experiments on its dev split and the one pre-registered run of its sealed
test split. The test split was scored once, at the final engine commit, and is reported
whatever the result.

## Identities

| Item | Value |
| --- | --- |
| Pre-registration | PR #157, merge `b3c08d516b80f2f03e3668fda00f8fec44b24a2e` (ledger `exp-0011`), before any `lab-v4` data |
| Dataset | `lab-v4b`, manifest SHA-256 `b9ae16b42749c0350e19897894ea4fd33b274b837e5e0e7191c5632a6bfe4ae7`, plan `EngineLabPlan/v2` table `v4b`, seed prefix `lab-v4b` |
| Collection code | `edfa0b5` (PR #162); re-collection with PR #163 (`1c56c6c`), the same tree plus `--seeds` |
| Final engine commit | `e906088` (PR #164): engine A-D with `engine.rca_prompt/v3`, stage E `engine.consistency/v2` policy `required_citations` |
| Model | `cmd/deepseek/deepseek-v4.1-flash` through `http://localhost:20128/v1`, cached by request digest |
| Baselines | P0 majority class; T0 signature rule (`engine-loop.py audit`); B2 detector (unchanged since `74d16ef`, so B2 at `e906088` is B2@`74d16ef`); B1 product path (`e06.context/v3`, `e06.rca_prompt/v3`); B4 engine |
| Ledger | `exp-0012` (lab-v4 rejected), `exp-0013` (lab-v4b accepted), `exp-0014` (keep), `exp-0015` (revert), `exp-0016` (this milestone) |

## Dataset attempts and trials

| Attempt | Planned | Collected | Outcome |
| --- | ---: | ---: | --- |
| `lab-v4` (table `v4`) | 334 | 71 (laptop 35, EC2 36) | Rejected on D4: dev T0 0.84 (21/25, Wilson lower bound 0.65), hard 16%. Stopped after two interleaved rounds; its 46 test trials were never read |
| `lab-v4b` (table `v4b`) | 338 | 338 (collecting host: laptop 137, EC2 201) | Accepted on D4: 0/0 leaks, dev T0 0.454, hard 54.6% (83/152) |

`lab-v4b` changes the scenario mix only, as the fail branch of D4 allows: a missing
readiness probe in seven trials of eight of every cell that does not need one, a slow
readiness warm-up on seven of eight traffic faults, and a Secret-reference look-alike
([dataset v2](../dataset-v2.md#attempt-2-lab-v4b)). Labels, D1-D5, sizes, splits,
dev-check and the host factor are unchanged.

Drops and relabels in `lab-v4b`:

- **Re-collected, 131 trials.** The engine-lab reader and monitoring tokens expire 8 hours
  after lab start. Every trial collected after that point had all Kubernetes slots
  unavailable (laptop 97 from 12:40Z, EC2 34 from 16:17Z). The rule
  "`kubernetes.state` unavailable" reads coverage only. These trials were moved out
  with their receipts and the same seeds (same splits) were collected again after
  `renew-readers`, 67 on each host; 33 laptop-planned seeds ran on EC2, recorded as
  `host: ec2` and `planned_host: laptop`.
- **Collection errors, retried.** Some trials failed before their window when a
  conditional Deployment patch lost a race with the controller (`docker operation failed`).
  The collection loop retried them; no partial trial was written.
- **Relabels.** None of the 338 accepted trials carries `silent`, `lookalike_cue_absent`
  or `herring_absent`; every decisive cue was observed.
- **Splits.** Dev 152 (dev-check 31), test 186 (in distribution 154, held out 32: `kv` 24,
  `mid` 8). No split drift.

## Dev: baselines and experiments

Dev-work has 121 trials, dev-check 31 (aggregate only).

| Dev-work | P0 | T0 | B2 | B1 | B4 (`1c56c6c`) | B4 (`e906088`) |
| --- | --- | --- | --- | --- | --- | --- |
| Family accuracy | 0.336 (all dev) | 0.454 (all dev) | 119/121 | 41/121 | 121/121 | 121/121 |
| Pass rate (family and citations) | - | - | - | - | 110/121 | 121/121 |
| Dev-check family accuracy / pass rate | - | - | 0.968 / - | 0.290 / - | 1.0 / 0.871 | 1.0 / 1.0 |

| Ledger | Stage | Change | Decision |
| --- | --- | --- | --- |
| `exp-0014` | E | `required_citations`: support aliases outside the answered family's required slots are removed after validation | Keep: hard-stratum pass +0.164 (trial bootstrap 95% CI 0.075-0.254), no other metric lower |
| `exp-0015` | D | `reasoning_effort=low` | Revert: hard-stratum paired latency difference CI includes 0 (median -0.39 s, CI -4.4 to +0.6 s); B4 p50 already 13.0 s |

The experiments were prepared on an interim snapshot before the D4 decision, at the
owner's direction, and decided only on the frozen dev-work split. A third prepared
change (prompt `engine.rca_prompt/v4`) targeted the same failure as `exp-0014` and was
not run. Phase 2 then stopped: M1-M4 are met by B4 on dev-work and dev-check (M4's
sealed-test part is below).

## Sealed test (`lab-v4b`, 186 trials, one run)

### Runs

| Run | Config | Dataset | Split | n | Commit | Model |
| --- | --- | --- | --- | ---: | --- | --- |
| `b4-lab-v4b-test-e906088` | B4  | `lab-v4b` (`b9ae16b42749`) | test | 186 | `e906088c923a` | cmd/deepseek/deepseek-v4.1-flash |
| `b1-lab-v4b-test-e906088` | B1  | `lab-v4b` (`b9ae16b42749`) | test | 186 | `e906088c923a` | cmd/deepseek/deepseek-v4.1-flash |
| `b2-lab-v4b-test-e906088` | B2  | `lab-v4b` (`b9ae16b42749`) | test | 186 | `e906088c923a` | - |

### Scorecards

| Metric | `b4-lab-v4b-test-e906088` | `b1-lab-v4b-test-e906088` | `b2-lab-v4b-test-e906088` |
| --- | --- | --- | --- |
| Family accuracy | 0.995 (185/186; 0.97-1.00) | 0.306 (57/186; 0.24-0.38) | 0.984 (183/186; 0.95-0.99) |
| Macro-F1 | 0.994 | 0.098 | 0.988 |
| Negatives false positive | 0.000 (0/55; 0.00-0.07) | 0.000 (0/55; 0.00-0.07) | 0.000 (0/55; 0.00-0.07) |
| Abstention precision | 0.982 (55/56; 0.91-1.00) | 0.299 (55/184; 0.24-0.37) | 0.948 (55/58; 0.86-0.98) |
| Abstention recall | 1.000 (55/55; 0.93-1.00) | 1.000 (55/55; 0.93-1.00) | 1.000 (55/55; 0.93-1.00) |
| Citation precision | 1.000 (696/696; 0.99-1.00) | 1.000 (18/18; 0.82-1.00) | 1.000 (6/6; 0.61-1.00) |
| Citation recall | 0.995 (416/418; 0.98-1.00) | 1.000 (8/8; 0.68-1.00) | 0.005 (2/408; 0.00-0.02) |
| Hallucinated aliases | 0 | 0 | 0 |
| First-attempt valid | 0.979 (182/186; 0.95-0.99) | 1.000 (14/14; 0.78-1.00) | - |
| Plan correct | 1.000 (186/186; 0.98-1.00) | 1.000 (186/186; 0.98-1.00) | 1.000 (186/186; 0.98-1.00) |
| Manual-only correct | 0.973 (36/37; 0.86-1.00) | 0.000 (0/37; 0.00-0.09) | 1.000 (37/37; 0.91-1.00) |
| Required-source recall (sent) | 1.000 (528/528; 0.99-1.00) | 0.057 (30/528; 0.04-0.08) | 0.057 (30/528; 0.04-0.08) |
| Message bytes p50 / max | 14929.0 / 17959 | 62286.0 / 65037 | 62286.0 / 65037 |
| Overflow at 65,536 | 0 | 84 | 84 |
| Latency p50 / p95 (ms) | 10877 / 23163 | 43604 / 67224 | None / None |
| Reasoning tokens p50 | 667.5 | 3939.5 | None |
| Failure tags | ABSTAIN_WRONG 1, BAD_CITATION 1 | ABSTAIN_WRONG 49, CONTEXT_OVERFLOW 69, PAIR_SPLIT 11 | BAD_CITATION 51, CONTEXT_OVERFLOW 69, PAIR_SPLIT 11 |

### Paired comparison `b1-lab-v4b-test-e906088` -> `b4-lab-v4b-test-e906088`

n paired = 186; base-only correct = 0; head-only correct = 128; exact McNemar p = 5.88e-39.

### Paired comparison `b2-lab-v4b-test-e906088` -> `b4-lab-v4b-test-e906088`

n paired = 186; base-only correct = 1; head-only correct = 3; exact McNemar p = 0.625.

T0 and P0 on test: family accuracy 0.489 and 0.296 (`engine-loop.py audit --split test
--milestone`); leak scan 0 and 0.

### Paired tests and intervals

| Baseline | B4-only correct | Baseline-only correct | Exact McNemar p | Paired difference (trial bootstrap 95% CI) |
| --- | ---: | ---: | --- | --- |
| B1 | 128 | 0 | 5.88e-39 | +0.688 (+0.624 to +0.753) |
| B2 | 3 | 1 | 0.625 | +0.011 (-0.011 to +0.032) |
| T0 | 95 | 1 | 2.45e-27 | +0.505 (+0.435 to +0.581) |
| P0 | 130 | 0 | 1.47e-39 | +0.699 (+0.634 to +0.763) |

B4 family accuracy trial bootstrap 95% CI: [0.9839, 1.0]

B1 family accuracy trial bootstrap 95% CI: [0.2419, 0.371]

B2 family accuracy trial bootstrap 95% CI: [0.9624, 1.0]

T0 family accuracy trial bootstrap 95% CI: [0.4194, 0.5591]

P0 family accuracy trial bootstrap 95% CI: [0.2312, 0.3602]

### Breakdowns

Family accuracy per cell, all five configurations. `lookalike: None` covers positives and
the healthy, herring and missing-source cases.

#### By stratum

| stratum | n | B4 | B1 | B2 | T0 | P0 |
| --- | ---: | --- | --- | --- | --- | --- |
| `easy` | 91 | 90/91 (0.989) | 36/91 (0.396) | 91/91 (1.000) | 91/91 (1.000) | 36/91 (0.396) |
| `hard` | 95 | 95/95 (1.000) | 21/95 (0.221) | 92/95 (0.968) | 0/95 (0.000) | 19/95 (0.200) |

#### By holdout

| holdout | n | B4 | B1 | B2 | T0 | P0 |
| --- | ---: | --- | --- | --- | --- | --- |
| `kv` | 24 | 24/24 (1.000) | 5/24 (0.208) | 24/24 (1.000) | 12/24 (0.500) | 4/24 (0.167) |
| `mid` | 8 | 8/8 (1.000) | 0/8 (0.000) | 8/8 (1.000) | 0/8 (0.000) | 0/8 (0.000) |
| `none` | 154 | 153/154 (0.994) | 52/154 (0.338) | 151/154 (0.981) | 79/154 (0.513) | 51/154 (0.331) |

#### By host

| host | n | B4 | B1 | B2 | T0 | P0 |
| --- | ---: | --- | --- | --- | --- | --- |
| `ec2` | 112 | 112/112 (1.000) | 36/112 (0.321) | 111/112 (0.991) | 51/112 (0.455) | 34/112 (0.304) |
| `laptop` | 74 | 73/74 (0.986) | 21/74 (0.284) | 72/74 (0.973) | 40/74 (0.541) | 21/74 (0.284) |

#### By lookalike

| lookalike | n | B4 | B1 | B2 | T0 | P0 |
| --- | ---: | --- | --- | --- | --- | --- |
| `None` | 151 | 150/151 (0.993) | 22/151 (0.146) | 148/151 (0.980) | 75/151 (0.497) | 20/151 (0.132) |
| `any` | 1 | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) |
| `crashloop` | 19 | 19/19 (1.000) | 19/19 (1.000) | 19/19 (1.000) | 0/19 (0.000) | 19/19 (1.000) |
| `oom` | 4 | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) |
| `pending` | 3 | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) |
| `rollout` | 2 | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) |
| `traffic` | 6 | 6/6 (1.000) | 6/6 (1.000) | 6/6 (1.000) | 6/6 (1.000) | 6/6 (1.000) |

#### By mechanism

| mechanism | n | B4 | B1 | B2 | T0 | P0 |
| --- | ---: | --- | --- | --- | --- | --- |
| `benign-rollout` | 2 | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) |
| `config-ref` | 10 | 10/10 (1.000) | 10/10 (1.000) | 10/10 (1.000) | 0/10 (0.000) | 10/10 (1.000) |
| `crash-after` | 7 | 7/7 (1.000) | 0/7 (0.000) | 7/7 (1.000) | 1/7 (0.143) | 0/7 (0.000) |
| `crash-setting` | 7 | 7/7 (1.000) | 0/7 (0.000) | 7/7 (1.000) | 3/7 (0.429) | 0/7 (0.000) |
| `crash-startup` | 8 | 8/8 (1.000) | 0/8 (0.000) | 8/8 (1.000) | 2/8 (0.250) | 0/8 (0.000) |
| `errors-mid` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |
| `errors-near` | 3 | 3/3 (1.000) | 0/3 (0.000) | 3/3 (1.000) | 1/3 (0.333) | 0/3 (0.000) |
| `errors-strong` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 1/4 (0.250) | 0/4 (0.000) |
| `healed-crash` | 2 | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) |
| `healed-oom` | 2 | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) |
| `healthy` | 5 | 5/5 (1.000) | 5/5 (1.000) | 5/5 (1.000) | 5/5 (1.000) | 5/5 (1.000) |
| `healthy-untrusted` | 3 | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) |
| `image-invalid` | 5 | 4/5 (0.800) | 0/5 (0.000) | 5/5 (1.000) | 5/5 (1.000) | 0/5 (0.000) |
| `image-never` | 5 | 5/5 (1.000) | 0/5 (0.000) | 5/5 (1.000) | 5/5 (1.000) | 0/5 (0.000) |
| `image-unreachable` | 5 | 5/5 (1.000) | 0/5 (0.000) | 5/5 (1.000) | 5/5 (1.000) | 0/5 (0.000) |
| `kv-crash-startup` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |
| `kv-errors-strong` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |
| `kv-healthy` | 4 | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) |
| `kv-image-unreachable` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 4/4 (1.000) | 0/4 (0.000) |
| `kv-oom-strong` | 4 | 4/4 (1.000) | 1/4 (0.250) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |
| `kv-pending-cpu` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 4/4 (1.000) | 0/4 (0.000) |
| `liveness-137` | 3 | 3/3 (1.000) | 0/3 (0.000) | 3/3 (1.000) | 3/3 (1.000) | 0/3 (0.000) |
| `liveness-clean` | 3 | 3/3 (1.000) | 0/3 (0.000) | 0/3 (0.000) | 0/3 (0.000) | 0/3 (0.000) |
| `load-ramp` | 3 | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) |
| `memory-near` | 4 | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) | 4/4 (1.000) |
| `missing-backoff` | 3 | 3/3 (1.000) | 0/3 (0.000) | 3/3 (1.000) | 0/3 (0.000) | 0/3 (0.000) |
| `missing-events-crash` | 1 | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) |
| `missing-events-pending` | 1 | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) |
| `missing-prometheus` | 2 | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) | 2/2 (1.000) |
| `oom-leak` | 6 | 6/6 (1.000) | 0/6 (0.000) | 6/6 (1.000) | 1/6 (0.167) | 0/6 (0.000) |
| `oom-mid` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |
| `oom-near` | 7 | 7/7 (1.000) | 0/7 (0.000) | 7/7 (1.000) | 2/7 (0.286) | 0/7 (0.000) |
| `oom-strong` | 8 | 8/8 (1.000) | 1/8 (0.125) | 8/8 (1.000) | 2/8 (0.250) | 0/8 (0.000) |
| `overload` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 1/4 (0.250) | 0/4 (0.000) |
| `pending-cpu` | 5 | 5/5 (1.000) | 0/5 (0.000) | 5/5 (1.000) | 5/5 (1.000) | 0/5 (0.000) |
| `pending-memory` | 5 | 5/5 (1.000) | 0/5 (0.000) | 5/5 (1.000) | 5/5 (1.000) | 0/5 (0.000) |
| `pending-selector` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 4/4 (1.000) | 0/4 (0.000) |
| `quota` | 3 | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) |
| `readiness-wrong` | 3 | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) | 3/3 (1.000) |
| `scale-zero` | 1 | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) | 1/1 (1.000) |
| `secret-ref` | 9 | 9/9 (1.000) | 9/9 (1.000) | 9/9 (1.000) | 0/9 (0.000) | 9/9 (1.000) |
| `slow-near` | 3 | 3/3 (1.000) | 0/3 (0.000) | 3/3 (1.000) | 0/3 (0.000) | 0/3 (0.000) |
| `slow-strong` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 1/4 (0.250) | 0/4 (0.000) |
| `throttle` | 4 | 4/4 (1.000) | 0/4 (0.000) | 4/4 (1.000) | 0/4 (0.000) | 0/4 (0.000) |


### Headline

- B4 family accuracy 185/186 (0.995; trial bootstrap 95% CI 0.984-1.000), pass rate
  184/186. Hard stratum 95/95, held-out `kv` 24/24 and `mid` 8/8, EC2 112/112 and laptop
  73/74.
- B4 beats B1 by 0.688 (CI 0.624-0.753), exact McNemar p = 5.9e-39 (128 against 0
  discordant). B4 against B2 is +0.011 (CI -0.011 to +0.032), p = 0.625: the detector
  alone nearly solves this dataset.
- Negatives false-positive rate 0/55 (look-alikes 0/35), hallucinated aliases 0,
  contrast-pair accuracy 24/24, distractor citation rate 0/696, untrusted-text flips 0/27,
  0 overflow, message p50 14.9 KB, analysis p50 10.9 s.
- The two B4 failures: one `image-invalid` trial answered `insufficient_evidence`
  (`ABSTAIN_WRONG`), and one held-out `kv-oom-strong` trial missed a required citation
  (`BAD_CITATION`).
- Host shift: B4 1.000 on EC2 and 0.986 on the laptop; T0 0.455 and 0.541; B1 0.321 and
  0.284.

### Phase 4 gate

| Condition | Result |
| --- | --- |
| B4 family accuracy at least B1's | 0.995 against 0.306: pass |
| 0 hallucinated aliases | 0: pass |
| Negatives false-positive rate 0 | 0/55: pass |

The gate passes, so the engine path goes into the product (phase 4).

## Cost

| Item | Value |
| --- | --- |
| Model, this goal up to this report | 538 calls and 3,481,301 tokens in replays (B4 test 190 calls, 1,066,360 tokens; B1 test 14 calls, 313,382 tokens), plus session probes of about 50 tokens each |
| AWS (one c5a.2xlarge engine lab, list price) | 18.9 running hours so far, about USD 5.8 instance plus about USD 0.2 for the gp3 volume and public IPv4; totals in the R4 evidence |

## Limitations

- **Dev lane, not qualifying.** These are offline replays of collected bundles on owned
  single-node labs. They do not qualify a deployment and are not E10 evidence.
- **One sample application.** There is no second application (D5); the
  out-of-distribution stratum holds out a log profile and an intensity level only.
- **Labels D1-D3 are design decisions.** Liveness kills are labelled `crashloop_startup`,
  `CreateContainerConfigError` and `ResourceQuota` `FailedCreate` are `unknown`.
- **The dataset no longer ranks strong engines.** B2 (no model) scores 183/186 and B4
  185/186; `lab-v4b` defeats the T0 shortcut (0.489) but not the detector. A further
  dataset needs harder look-alikes for the detector itself.
- **Attempt history.** `lab-v4` was rejected after 71 trials on an interim D4 estimate.
  131 `lab-v4b` trials were re-collected after a reader-token expiry; the rule and the
  moved trials are retained privately.
- **CI.** PRs #152-#154 merged while CI was disabled (later validated green on main
  `8c1cb36`). In this loop the owner disabled CI again: PRs #162-#164 merged on local ruff
  and focused tests.
- **M5 is still open.** It is measured by the R4 campaign.
