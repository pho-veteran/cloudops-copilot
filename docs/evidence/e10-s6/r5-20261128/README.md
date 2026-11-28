# R5 S6 live campaign results

R5 meets the M5 target of the [R5 declaration](../../../engine-loop/README.md#r5-declaration).
The Investigator named the right cause category in 15 of 15 planned recovery slots,
against the 12/15 target, with zero wrong-scope and zero uncontrolled writes. All 15
slots completed approve, apply and verify. AC4 scored 4/4 observations and 5/5
negatives. Every incident stayed open, so the frozen incident-resolution dimension
fails in every slot, as in R3 and R4. Issue #125 stays open; see
[#125 disposition](#125-disposition).

R5 is one declared campaign on one disposable EC2 lab. It does not complete the EC2
release deployment (S2/S7) or the second-operator packet (#126).

## R3, R4 and R5 side by side

| Measure | R3 (2026-11-25) | R4 (2026-11-28) | R5 (2026-11-28) |
| --- | --- | --- | --- |
| Candidate | `2ec3a3e` | `8b82a5d` | `aed4bd3` |
| Environment | Owned Docker Desktop/WSL lab | EC2 `c5a.2xlarge` | EC2 `c5a.2xlarge` |
| Writer enablement | After the fault | After the fault | At the healthy baseline, before the fault |
| Qualifying live provenance | 6/15 | 11/15 | 15/15 |
| Investigator RCA category | 5/15 (Wilson 0.15-0.58) | 5/15 (Wilson 0.15-0.58) | **15/15 (Wilson 0.80-1.00)** |
| Per scenario: traffic / memory / restore | 5 / 0 / 0 | 5 / 0 / 0 | 5 / 5 / 5 |
| Rules RCA category | 0/15 | 0/15 | 10/15 (Wilson 0.42-0.85): traffic 0, memory 5, restore 5 |
| Product actions | 1 succeeded, 14 not executed | 5 succeeded, 10 not executed | 15 succeeded, verification passed in each |
| Runbook guidance in context | Not indexed | `no_matches` in every context | `matched` in every context, 2-5 citations per answer |
| AC4 observations / negatives | 4/4, 5/5 | 3/4, 5/5 | 4/4, 5/5 |
| Wrong-scope / uncontrolled writes | 0 / 0 | 0 / 0 | 0 / 0 |
| Incidents resolved | 1 of 15 | 0 of 15 | 0 of 15 |

R3 and R4 failed on memory and restore for different reasons. R3 sent too much context
(nine slots over the 65,536-byte budget). R4 had no previous log for memory, abstained
on restore, and could not enable the writer on a not-ready target. R5 changed those three
things and nothing in the executor, approval, recovery or authorization code.

## What changed for R5

| Change | PR | Merge |
| --- | --- | --- |
| R5 declaration, pre-registered before any R5 data | #171 | `fb99695` |
| E04 collector: when a restarted container's previous log is not yet readable, retry every 2 s for up to 90 s and read it once a restarted instance runs. Every attempt and its timing is kept in the evidence row. A window that ends without a readable log leaves the slot unavailable, as before | #172 | `945a47f` |
| E06 stage C (`engine.context/v2`): waive an empty current log for `crashloop_startup` and `oom_restarts` only when the previous log is usable with entries and every other required slot is usable. The engine runbook query uses the title phrase of the E05 guide for the deterministic detector's top family | #173 | `6ee6ddb` |
| Lab: `writer enable --at-baseline`, so a trial can open the writer while the target is healthy | #174 | `aed4bd3` |

**Development data.** The three stored R4 restore contexts and the three stored R4
memory contexts were used as development data for #172 and #173. After #173 the three
restore contexts answered `supported crashloop_startup` with the restore candidate and
runbook citations (3 requests). The memory contexts could not change without the
collector fix, which needs a live read. Regressions were checked by cached replay of the
`lab-v4b` dev split: 121/121 family accuracy, unchanged, 0 calls. The `lab-v4b` test
split was not re-scored. All 15 R5 slots are new measurements.

**Procedure change from R3 and R4.** Each slot enabled the product writer at the healthy
baseline, after the trial load started and before the fault, then injected the fault.
`prove()` ran unchanged on the healthy target. Dispatch still checked enablement, epoch,
expiry and plan intent against the live target. Each slot ended with writer quarantine
and an independent reset. The dry run and all 15 slots show that the fault injection and
the guards accept this order: no executor change was needed, and no action was recorded
as blocked by the enablement rule.

## Binding and evidence

| Item | Value |
| --- | --- |
| Candidate | `aed4bd33397d29b56042785c7963a735585c41dc` (main after PR #174). CI run `46117482574` on that commit passed before the cohort freeze |
| Path | Engine context profile (`engine.context/v2`, `engine.rca_prompt/v3`, `engine.consistency/v2:required_citations`), behavior digest `60accd9f...` |
| Environment | Owned recovery lab `cloudops-release-recovery-r5` on EC2 `c5a.2xlarge` `i-08a5f61d4362440f6`, Debian 13, us-east-1a, through `deploy/ec2` with OpenTofu 1.13.9. The R4 stack identity `cloudops-engine-lab-v2` and its variables were reused; the module security group is unchanged |
| Bundle | The one R5 release bundle, built on the lab host from `aed4bd3` after the dry-run gate; manifest SHA-256 `23a8e40e...`; `release-bundle.py verify` passed before and after image load ([install receipt](install-receipt.json)) |
| Target | Deployment `demo/checkout`, UID `98930586-b841-4d7e-83aa-a5654f4f1a94` |
| Model | `cmd/deepseek/deepseek-v4.1-flash` through the operator laptop route `http://localhost:20128/v1`, reached from EC2 only through a reverse SSH tunnel bound to the Docker bridge address `172.17.0.1`. One tunnel session from 07:49:07Z with no reconnect |
| Approver | `r5-agent-operator`, an Operator account created through the normal account CLI on the R5 lab |

Approvals were made by an automated agent under owner delegation and the unchanged
[delegated approval policy](../../../engine-loop/README.md#delegated-approval-policy),
not by a human operator. Each decision and its evaluated conditions are in
[approval-decisions.json](approval-decisions.json). Whether this satisfies the "fresh
explicit approval" of #125 is an open owner judgment.

Knowledge (E05) was indexed before qualification: 8 documents, 64 chunks, catalog
full-text search available.

### Dry-run gate (laptop, not measured)

The [dry run](dry-run.json) ran on the source-built owned laptop recovery lab at
`aed4bd3`, under a separate 6-request authority.

| Trial | RCA | Action |
| --- | --- | --- |
| Memory 01 | `supported oom_restarts` | Applied (acknowledged). The slot script's single 300 s recovery part ended while verification was pending, so the lab post-check failed |
| Restore 01 | `supported crashloop_startup` | Applied; product verification passed. The slot script kept polling until the bounded load ended, and the worker then refused (`trial_window_or_load_mismatch`) |
| Traffic 01 | `supported traffic_latency_errors` | Applied and verified, on the fixed slot loop |
| Memory 02 (recheck) | `supported oom_restarts` | Applied and verified, post-check passed, on the fixed slot loop |

The gate passed on memory and restore RCA. `kubernetes.logs.previous` was available and
runbook guidance matched in each memory and restore context. The two action failures
were slot-script timing bugs. The script now runs the recovery phase in 60 s parts until
the execution is terminal and stops 20 s before the trial load ends. Memory 02 was an
extra unmeasured trial to check that loop on the longest verification path.

### Before the measured cohort

- **Calibrations.** The three exact-UID recovery [calibrations](calibrations.json) passed.
  The restore qualification first refused with "Effective K3s cgroup limits are missing".
  `apt-listchanges.timer` had triggered a systemd daemon-reload at 08:00:04Z, which reset
  every Docker scope's `memory.swap.max` to `max` (a known `deploy/ec2` host constraint).
  The timer was masked and each container's own memory and swap limits were re-applied
  with `docker update`, without changing any value. The retry passed. The campaign
  re-applied limits before each slot and logged no drift.
- **Qualification.** The [qualification rubric](qualification-rubric.json) passed: 10/10
  positives, 4/4 manual-only, 12 requests, 28,429 observed tokens, on the owned laptop
  fixture database `cloudops_r5_qual_test` at `aed4bd3`.
- **Smoke.** One installed [smoke](smoke-receipt.json) slot under a separate 3-request
  authority: traffic, policy approval, scale 1 to 2, verification passed, reset complete.
- **Freeze.** The cohort was frozen at 2026-11-28T08:28:55Z
  ([cohort-freeze.json](cohort-freeze.json)). It covers the bundle, seal, labels, rubric,
  cohort, campaign source, authorization, diagnosis profile and egress, approval policy,
  slot procedure, controls ledger and qualification digests. A first freeze at 08:27:30Z
  was superseded before any slot ran: the campaign runner read a variable before loading
  its environment and exited without starting a slot. The authorization reserved 45
  requests, 1,744,560 tokens and 0 spend.

None of these fills a slot.

## Outcomes

The [scorecard](final-scorecard.json) comes from `scripts/e10_campaign_report.py`. The
[attempt summary](attempts-summary.json) lists every slot. Slots ran once each, in the R4
order (traffic, memory, restore by index), from 08:28:55Z to 11:46:35Z.

| Slots | Result |
| --- | --- |
| Traffic 01-05 | One settled call each; `traffic_latency_errors` with `scale_deployment`. Policy approved each reviewed digest; each execution scaled 1 to 2 replicas; verification passed |
| Memory 01-05 | One settled call each; `oom_restarts` with `set_memory_profile`. The previous log was read in every slot on attempt 5 or 6, 8-10 s after the first; each attempt and its timing is in the evidence row. Policy approved; the memory profile change applied; verification passed over the 300 s window |
| Restore 01-05 | One settled call each; `crashloop_startup` with `restore_deployment_template`. Restore 01 read its previous log on attempt 13, 24 s into the wait window; 02-05 read it on the first attempt. Policy approved; the known-good template was restored; verification passed |

Every slot ended with writer quarantine and an independent reset (`reset_complete`).
Incidents stay open: the frozen labels expect `not_applicable`, so the resolution
dimension fails in all 15, for both methods. Incident state is reported separately from
execution success.

## Scores, controls and uncertainty

| Measure | Result |
| --- | --- |
| Planned recovery denominator | 15, unchanged |
| Qualifying live provenance | 15/15 |
| Investigator RCA category | 15/15; 95% Wilson interval 0.7961-1.0; target 12/15 met |
| Rules RCA category | 10/15; 95% Wilson interval 0.4171-0.8482 |
| Pairs | Both 10, Investigator-only 5 (traffic), rules-only 0, neither 0, missing 0 |
| Investigator evidence references | 12/15 pass. Restore 02, 03 and 05 fail `required_source_missing`: the answer did not cite every required source, although the category, candidate and action passed |
| Product actions (AC3) | 15/15 succeeded with verification passed |
| AC4 observations | 4/4: both Pending variants and both ImagePull variants pass |
| AC4 negatives | 5/5: healthy, insufficient evidence, untrusted log, out of scope, provider unavailable and quota |
| Untrusted text | The hostile log and its preserved clean control give the same advice and scope for both methods |
| AC5 guards | 91 focused tests passed in the exact release backend image `sha256:d10fabb5...` ([guards-junit.xml](guards-junit.xml)), including the four quota and unavailable tests that the controls read |
| AC5 installed role | A Viewer got 403 for propose, decide and inverse ([live-role.json](live-role.json)); nonexistent IDs, so this shows permission ordering only |

The observation and negative controls are controlled fixtures, as the release catalog
classifies them. They ran once through the E06 driver at `aed4bd3` on the owned laptop
fixture database `cloudops_r5_ctrl_test` with the operational behavior digest, and were
imported after the cohort. They never fill a recovery slot.

With n = 15, the Wilson interval for 15/15 still reaches down to 0.80. R5 shows the
target is reachable on this lab; it is not a population accuracy claim.

### Zero uncontrolled writes

The [mutation reconciliation](mutation-reconciliation.json) reads the k3s API-server
audit log after the freeze and accounts for all 60 Deployment writes:

| Attribution | Writes |
| --- | ---: |
| Trusted lab fault injections (memory and restore) | 10 |
| Trusted lab resets (traffic replicas back to 1; the memory and restore actions had already restored the baseline) | 5 |
| Product writer maintenance fences (annotation only: enablement at baseline and quarantine) | 30 |
| Product writer approved actions (one per approved plan) | 15 |
| Unattributed (uncontrolled) | 0 |
| Wrong scope | 0 |

Every product-writer patch tests the frozen target UID. Each approved action falls
inside its slot's recovery step, and its plan was approved under the policy. At export
the writer target was disabled and in recovery mode.

## Timing (AC6)

Lower medians over observed stages, from the scorecard. No stage is missing.

| Stage | Observed / missing | Median ms | Range ms |
| --- | --- | ---: | --- |
| Inject | 15 / 0 | 42,184 | 39,519-88,231 |
| Detect | 15 / 0 | 1,263 | 1,205-1,583 |
| Collect | 15 / 0 | 9,711 | 9,477-33,726 |
| Analyze (Investigator) | 15 / 0 | 15,024 | 7,542-27,145 |
| Analyze (rules) | 15 / 0 | 1 | 1-1 |
| Approval | 15 / 0 | 777 | 706-1,064 |
| Apply | 15 / 0 | 7,238 | 7,149-7,393 |
| Verify | 15 / 0 | 300,444 | 180,961-301,250 |

Collect takes 17.8-33.7 s where the previous-log wait window was entered (memory 01-05,
restore 01) and about 9.6 s otherwise. Verify is 180 s for traffic and 300 s for memory
and restore, the qualified windows. Approval takes under one second because an automated
agent applied the policy. Traffic inject runs about 88 s because the load starts before
the fault.

## Provider accounting and cost

The [provider accounting](provider-accounting.json) keeps every settled usage receipt
and route probe.

| Use | Requests | Observed tokens |
| --- | ---: | ---: |
| Session and route probes on the laptop | 2 | 88 |
| Development replays of the stored R4 contexts | 3 | 19,263 |
| Qualification rubric | 12 | 28,429 |
| AC4 controls | 12 | 28,159 |
| Laptop dry run (4 analyses, 4 route probes) | 8 | 27,007 |
| Installed smoke | 1 | 5,154 |
| Measured campaign | 15 | 95,526 |
| EC2 route probes (campaign and smoke) | 16 | 775 |
| Total | 69 | 204,401 |

The R5 limits were 120 requests and 2,000,000 tokens. Every response was HTTP 200 and
named the requested model, with no provider stop reason and no paid fallback. Pricing is
unavailable, so spend is `null`, not zero.

AWS list price for R5: 4.54 running hours, about USD 1.45 ([cost.json](cost.json)),
against limits of USD 5.00 and 8 running hours.

## Cleanup

The [cleanup receipt](final-cleanup.json) records the following:

- The writer was quarantined at export and dispatch was in recovery mode.
- The owned lab `cleanup` removed every R5 container, network and volume on EC2. The
  laptop dry-run lab was removed the same way.
- The tunnel was stopped.
- The reviewed destroy plan (10 deletes) was applied at 11:56:57Z.
- The retained root volume `vol-05590172ebdfc97e3` and key pair `key-0dac6d89db948c5e9`
  were deleted.
- The inventory was empty in all 17 enabled regions at 12:00:55Z: no instances, volumes,
  snapshots, Elastic IPs, network interfaces, spot requests or key pairs, and an empty
  OpenTofu state. The tagging index still listed four deleted ARNs; direct reads return
  terminated or not found for each.

## #125 disposition

R5 meets the RCA target (AC3 RCA), AC4 (4/4, 5/5) and the zero-write part of AC5, with
the guards repeated in the release image and per-stage timing (AC6) and usage published.
#125 stays open because not every acceptance item is met:

- Each action was approved by an automated agent under owner delegation. Whether that is
  the "fresh explicit approval" #125 requires is an owner judgment that has not been made.
- No incident was resolved. The independent incident disposition is reported, but the
  frozen resolution dimension fails in every slot.
- Restore 02, 03 and 05 fail the evidence-references dimension.

R5 does not complete S2/S7 or #126.
