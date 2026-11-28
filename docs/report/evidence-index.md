# Evidence index

Part of the [capstone report](README.md). Each row maps a claim to the file that
supports it. Receipts under `docs/evidence/` are immutable. See the
[evidence folder index](../evidence/README.md) for every receipt set.

## Diagnosis engine (offline)

| Claim | Source |
| --- | --- |
| R3 failure analysis and engineering targets | [engine-loop README](../engine-loop/README.md#baseline-r3-live-campaign), [R3 context diagnostics](../evidence/e10-s6/r3-20261125/final-context-diagnostics.json) |
| Engine stages A-E and their versions | [algorithm](../engine-loop/algorithm.md), [core diagnosis design](../system-design/core-diagnosis.md) |
| `lab-v3` results, and B4 beating B1 on its sealed test | [M0-M4 report](../engine-loop/reports/m0-m4.md) |
| `lab-v3` saturation and the T0 signature rule | [dataset v2 design](../engine-loop/dataset-v2.md#why-lab-v3-cannot-drive-the-loop) |
| Pre-registration of loop v2, R5 and the approval policy | [engine-loop README](../engine-loop/README.md) (PRs #157 and #171) |
| `lab-v4b` acceptance, dev experiments and the sealed test | [loop v2 report](../engine-loop/reports/v2-lab-v4.md) |

## Live campaigns

| Claim | Source |
| --- | --- |
| R3: 5/15, context over budget | [R3 README](../evidence/e10-s6/r3-20261125/README.md), [scorecard](../evidence/e10-s6/r3-20261125/final-scorecard.json) |
| R4: 5/15, writer-enable diagnosis | [R4 README](../evidence/e10-s6/r4-20261128/README.md) |
| R5: 15/15, actions, AC4, AC6 timing | [R5 README](../evidence/e10-s6/r5-20261128/README.md), [scorecard](../evidence/e10-s6/r5-20261128/final-scorecard.json) |
| R5 approval decisions | [approval-decisions.json](../evidence/e10-s6/r5-20261128/approval-decisions.json) |
| R5 guards in the release image | [guards-junit.xml](../evidence/e10-s6/r5-20261128/guards-junit.xml) |
| R5 model usage and no paid fallback | [provider-accounting.json](../evidence/e10-s6/r5-20261128/provider-accounting.json) |
| Owner decision closing #125 | [#125 comment](https://github.com/pho-veteran/cloudops-copilot/issues/125#issuecomment-7241234383) |

## Product features

| EPIC | Behaviour | Acceptance and evidence |
| --- | --- | --- |
| E00 Foundation | [Foundation handoff](../guides/foundation.md) | [E00 #15](https://github.com/pho-veteran/cloudops-copilot/issues/15) |
| E01 Access | [Local integration lab](../guides/local-lab.md) | `docs/evidence/e01-*` |
| E02 Workloads | [Scope](../features/workloads/scope.md), [observation](../features/workloads/observation.md), [baselines](../features/workloads/baselines.md) | [Acceptance](../acceptance/e02-workloads.md), [browser evidence](../evidence/e02-browser/) |
| E03 Incidents | [Alert intake](../features/incidents/alert-intake.md), [admission](../features/incidents/admission.md) | [Admission](../acceptance/e03-incident-admission.md), [integration](../acceptance/e03-incident-integration.md), [evidence](../evidence/e03-integration/) |
| E04 Investigations | [Kubernetes](../features/investigations/kubernetes-evidence.md), [Prometheus](../features/investigations/prometheus-metrics.md), [dispatch](../features/investigations/job-dispatch.md), [comparison](../features/investigations/comparison.md) | [Acceptance](../acceptance/e04-evidence.md), [evidence](../evidence/e04-integration/) |
| E05 Knowledge | [Corpus](../features/knowledge/runbook-corpus.md), [index](../features/knowledge/runbook-index.md), [guidance](../features/knowledge/run-guidance.md), [UI](../features/knowledge/ui.md) | [knowledge58](../evidence/knowledge58/) to [knowledge62](../evidence/knowledge62/README.md) |
| E06 Diagnosis | [Context](../features/diagnosis/context.md), [lifecycle](../features/diagnosis/lifecycle.md), [model client](../features/diagnosis/model-client.md), [findings](../features/diagnosis/findings.md) | [Model acceptance](../acceptance/e06-model-acceptance.md), [E06 R5 ledger](../evidence/e06-r5/README.md) |
| E07 Recovery | [Verification](../features/recovery/verification.md) | [Acceptance](../acceptance/e07-recovery.md), [E07 ledger](../evidence/e07-integrated.json) |
| E08 Resources | [Resource reports](../features/resources/reports.md) | [E08 integrated](../evidence/e08-r6-integrated.md) |
| E09 Connections | [Source checks](../features/connections/checks.md) | [E09 integrated](../evidence/e09-integrated.md) |
| E10 Evaluation and release | [Release package](../guides/release-package.md), [restore](../guides/release-restore.md), [EC2 lab](../../deploy/ec2/README.md) | [S1](../evidence/e10-s1-release/README.md), [S2](../evidence/e10-s2-ec2/README.md), [S4 baseline](../acceptance/e10-evaluation-baseline.md), [S5](../evidence/e10-s5-restore/README.md), [S6](../evidence/e10-s6/) |
