# Documentation

Start with the [capstone report](report/README.md) for what was built, how it was
measured and what the results are. The rest of this folder is organized by purpose.

| Folder | Purpose | Audience |
| --- | --- | --- |
| [report/](report/README.md) | Capstone report: summary, results, limitations, and an index from each claim to its evidence | Report readers, reviewers |
| [system-design/](system-design/README.md) | Architecture, domain, data model, interfaces, core diagnosis and recovery design | Reviewers, contributors |
| [engine-loop/](engine-loop/README.md) | Diagnosis engine improvement loop: algorithm, datasets, protocol, pre-registrations and milestone reports | Contributors, reviewers |
| [features/](#features) | Behaviour and contract of each implemented feature, grouped by EPIC | Contributors |
| [guides/](#guides) | How to set up, run, release and restore | Operators, contributors |
| [acceptance/](#acceptance) | Acceptance procedures and records for EPIC slices | Reviewers |
| [evidence/](evidence/README.md) | Immutable receipts, screenshots and campaign results | Reviewers |
| [images/](images/) | Screenshots referenced by feature and acceptance pages | - |

Scope, decisions and acceptance live in the
[system tracker #3](https://github.com/pho-veteran/cloudops-copilot/issues/3) and its
EPICs. These pages describe the implemented behaviour.

## Features

| EPIC | Pages |
| --- | --- |
| E02 Workloads | [Scope](features/workloads/scope.md) · [Observation](features/workloads/observation.md) · [Healthy baselines](features/workloads/baselines.md) |
| E03 Incidents | [Alertmanager intake](features/incidents/alert-intake.md) · [Request admission](features/incidents/admission.md) |
| E04 Investigations | [Kubernetes evidence](features/investigations/kubernetes-evidence.md) · [Prometheus metrics](features/investigations/prometheus-metrics.md) · [Collection checkpoints and job dispatch](features/investigations/job-dispatch.md) · [Run comparison and citations](features/investigations/comparison.md) |
| E05 Knowledge | [Runbook corpus](features/knowledge/runbook-corpus.md) · [Runbook index](features/knowledge/runbook-index.md) · [Run guidance](features/knowledge/run-guidance.md) · [Knowledge UI](features/knowledge/ui.md) |
| E06 Diagnosis | [Context and findings contract](features/diagnosis/context.md) · [Durable lifecycle](features/diagnosis/lifecycle.md) · [Model client](features/diagnosis/model-client.md) · [Reasoning effort](features/diagnosis/reasoning-effort.md) · [Inspecting findings](features/diagnosis/findings.md) |
| E07 Recovery | [Qualified verification](features/recovery/verification.md) |
| E08 Resources | [Resource reports](features/resources/reports.md) |
| E09 Connections | [Source checks](features/connections/checks.md) |

## Guides

| Guide | Use it to |
| --- | --- |
| [Foundation handoff](guides/foundation.md) | Run the local application, the PostgreSQL checks and the UI journeys |
| [Local integration lab](guides/local-lab.md) | Run the disposable HTTPS/K3s lab with scoped readers and the executor |
| [Triage demo](guides/triage-demo.md) | Run the repeatable incident scenarios |
| [Release package](guides/release-package.md) | Build and install the pinned HTTPS release |
| [Release backup and restore](guides/release-restore.md) | Back up and restore a release installation |
| [EC2 lab](../deploy/ec2/README.md) | Provision and tear down the single-host EC2 lab |

## Acceptance

| EPIC | Record |
| --- | --- |
| E02 | [Integrated workload acceptance](acceptance/e02-workloads.md) |
| E03 | [Request admission](acceptance/e03-incident-admission.md) · [Integrated incident acceptance](acceptance/e03-incident-integration.md) |
| E04 | [Integrated evidence acceptance](acceptance/e04-evidence.md) |
| E06 | [Finite model acceptance](acceptance/e06-model-acceptance.md) |
| E07 | [Owned recovery acceptance](acceptance/e07-recovery.md) |
| E10 | [S4 evaluation baseline and scoring rubric](acceptance/e10-evaluation-baseline.md) · [S6 live campaigns](evidence/e10-s6/) |

## Conventions

- **Evidence is never edited after capture.** A receipt keeps the paths and digests of its
  time. Paths inside older receipts may name a page before it moved to `features/`,
  `guides/` or `acceptance/`.
- **Claim levels.** Every result is labelled as fixture-tested, offline (replayed lab
  bundles), local live or EC2 live. One level is never presented as another.
- **Language.** Authored pages are in English. Quoted human input keeps its original
  language.
