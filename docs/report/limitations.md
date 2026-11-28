# Limitations and disclosures

Part of the [capstone report](README.md).

## What the results do not show

- **Generalization beyond the lab.** Every offline and live result comes from one small
  sample application on a single-node K3s lab, with six fault kinds and their
  look-alikes. A second application was not used (decision D5). The held-out log
  profile and intensity level are the only out-of-distribution strata.
- **A significant engine gain over the detector.** On the sealed `lab-v4b` test, B4
  scored 185/186 and the deterministic detector B2 scored 183/186 (p = 0.625). The engine
  adds cited confirmation, abstention and complete findings. It does not add measurably
  higher family accuracy on this dataset.
- **A significant live gain over rules.** In R5 the Investigator scored 15/15 and the
  rules baseline 10/15. On 15 paired slots that is exact McNemar p = 0.0625.
- **The release deployment and second-operator workflows.** R4 and R5 ran on disposable
  EC2 labs. The EC2 release qualification (S2/S7) and the second-operator packet (#126)
  remain separate open work.
- **Human-in-the-loop approval in the campaigns.** R4 and R5 approvals were made by an
  automated agent under owner delegation (see below). No campaign measured human
  approval latency or behaviour.

## Deviations and disclosures

| Item | Disclosure |
| --- | --- |
| Agent-made approvals | In R4 and R5 a dedicated Operator account approved plans under a deterministic, pre-registered policy. The owner decided, through a delegate, that this satisfies "fresh approval" for the owned-lab campaign ([#125 decision](https://github.com/pho-veteran/cloudops-copilot/issues/125#issuecomment-7241234383)). The product default remains human approval |
| Writer enablement order | R5 enables the writer at the healthy baseline, before fault injection. R3 and R4 enabled it after the fault, which the executor's readiness guard refuses for not-ready targets. Guard code is unchanged |
| Development data for R5 | The R5 fixes were developed on the stored R4 memory and restore contexts. R5 trials are new measurements |
| Dataset attempts | `lab-v4` was rejected on D4 after 71 trials and stopped early. `lab-v4b` changed the scenario mix after the `lab-v4` dev audit, as the pre-registered fail branch allows, and was given a new sealed test split |
| Re-collected trials | 131 `lab-v4b` trials were collected again with the same seeds after the lab reader tokens expired. Their earlier copies had every Kubernetes slot unavailable |
| CI during the loop | PRs #152-#154 merged while CI was disabled. From PR #162 the owner disabled CI to speed up iteration, and PRs merged on recorded local checks. The final CI runs on `main` (`8c1cb36`, `0eaf619`) passed |
| Ledger timestamps | Ledger `exp-0015` is dated after the sealed-test milestone `exp-0016`, but its run finished before the test runs started |
| Labels D1-D3 | The labels for a liveness-probe kill (`crashloop_startup`), `CreateContainerConfigError` (`unknown`) and quota `FailedCreate` (`unknown`) are design decisions, recorded before data collection |

## Findings that remain

- **Incident resolution.** Incidents stay open after a verified recovery, so the frozen
  incident-resolution scoring dimension fails in every R3-R5 slot. The disposition is
  recorded separately from execution success.
- **Restore citations.** R5 restore 02, 03 and 05 pass category, candidate and action,
  but fail the evidence-references dimension (`required_source_missing`).
- **Executor readiness rule.** The guard refuses writer enablement on a not-ready target.
  Product writes for a crashing workload therefore depend on the writer being enabled
  while the target was healthy. Whether a qualified path for not-ready targets should
  exist is an E07 design question.
- **Offline-to-live gap.** The dataset generator collects within seconds of a container
  restart. Before PR #172, the product collector did not, which hid the missing
  previous log until R4. Dataset trials that use the product's collection timing would
  keep this gap visible.
