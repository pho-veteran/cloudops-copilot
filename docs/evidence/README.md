# Evidence

Receipts, screenshots and campaign results captured when each slice was accepted. They are
**immutable**: a receipt keeps the paths, digests and wording of its capture time and is
never edited or regenerated. Where two receipts disagree, the newer receipt names the
older one and explains the difference. A receipt is never rewritten to match.

Paths inside older receipts may name documentation pages at their earlier locations
(`docs/<name>.md`). Those pages now live under `docs/features/`, `docs/guides/` and
`docs/acceptance/`; see the [documentation index](../README.md). The frozen copies under
`e10-s4/source/` keep their original links on purpose.

The [capstone evidence index](../report/evidence-index.md) maps each reported claim to
its receipt.

## By EPIC

| EPIC | Receipts |
| --- | --- |
| E00 Foundation | `foundation-*.jpg` |
| E01 Access and HTTPS lab | `e01-*` |
| E02 Workloads | `scope26-*` (#26), `observation27-*` (#27), `baseline28-*` (#28), `e02-*`, [e02-browser](e02-browser/) |
| E03 Incidents | `intake36-*` (#36), [incident-handoff](incident-handoff/), [work-queue](work-queue/), [e03-integration](e03-integration/) |
| E04 Investigations | [evidence-storage](evidence-storage/) (#45), [job-dispatch](job-dispatch/) (#46), [kubernetes47](kubernetes47/), [prometheus48](prometheus48/), [comparison49](comparison49/), [e04-integration](e04-integration/) |
| E05 Knowledge | [knowledge58](knowledge58/) (corpus), [knowledge59](knowledge59/README.md) (index), [knowledge60](knowledge60/) (guidance), [knowledge61](knowledge61/) (UI), [knowledge62](knowledge62/README.md) (history and degraded journeys) |
| E06 Diagnosis | [e06-r1-model-client](e06-r1-model-client/README.md), [e06-r2-context](e06-r2-context/) (also unit-test fixtures), [e06-r3-lifecycle](e06-r3-lifecycle/), [e06-r4-ui](e06-r4-ui/), [e06-r5](e06-r5/README.md) (qualified cohort) |
| E07 Recovery | `e07-integrated.json` (bound by `config/lab/release-scenarios-v1.json`), `e07-model-requalification.json`, `e07-r4`..`e07-r6-*` |
| E08 Resources | `e08-r1`..`e08-r5-*`, [e08-r6-integrated.md](e08-r6-integrated.md) |
| E09 Connections | [connections105](connections105/) (#105), [e09-integrated.md](e09-integrated.md), [e09-operations.md](e09-operations.md) |
| E10 Evaluation and release | [S1 release](e10-s1-release/README.md), [S2 EC2](e10-s2-ec2/README.md), [S3 scenarios](e10-s3/README.md), [S4 frozen baseline](e10-s4/README.md), [S5 restore](e10-s5-restore/README.md), [S6 campaigns R3-R5](e10-s6/README.md) |

## Files that other code reads

Moving or renaming these files breaks code or bindings:
- `e06-r2-context/` is read by the diagnosis unit tests.
- `e07-integrated.json` is named by the scenario catalog and its sealed oracle.
- `job-dispatch/api.json` and `e06-r1-model-client/` are the default outputs of their probe scripts.
