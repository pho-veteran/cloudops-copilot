# E10 S7 release packet

[#126](https://github.com/pho-veteran/cloudops-copilot/issues/126) ·
[E10 acceptance and decisions](https://github.com/pho-veteran/cloudops-copilot/issues/13) ·
[Second-operator walkthrough](../../release-second-operator.md)

**Packet status:** Preparation. The run sheet, demo script, evidence index,
local core rehearsal and handoff are ready. Final candidate, deployed
walkthrough, S6 measurement report and demo recording are pending.

**Evidence baseline:** Source inspected at `887197c5880a328be3e2c78738e2ddcf4b387bf5`
on 2026-11-25. The [S2 qualification](../e10-s2-ec2/README.md) used
`6ac5bf9` on EC2 and the host was destroyed. The
[S5 qualification](../e10-s5-restore/README.md) used `f8e5dba` in disposable
local projects. [S6 tooling](https://github.com/pho-veteran/cloudops-copilot/pull/141)
has merged. The [S6 ledger](../e10-s6/README.md) records zero counted live trials
at this baseline.

**Current deployment instructions:** [#142](https://github.com/pho-veteran/cloudops-copilot/pull/142)
added the version 2 [owned recovery lab](../../release-package.md#owned-recovery-lab-with-release-images).
It supplies the release-bound alert sender and KRR runner. The exact deployed
delivery and the release-project binding of the bounded KRR driver remain open.
The historical preparation receipt keeps its original version 1 bundle binding.

## Acceptance map

Use the names in the receipt column when attaching sanitized evidence. Every
new receipt should state its collection date, candidate, environment, scope,
evidence class and result. Keep an explicit reason and remaining proof for
each open criterion.

| Criterion | Existing input and evidence class | Release packet receipt | Open at preparation |
| --- | --- | --- | --- |
| AC1 · Rules comparison | [S4 controlled evaluation](../e10-s4/README.md) | `measurement`: sealed source/cohort/scorer and paired Rules/Investigator outcomes on matching evidence/Knowledge bundles | Accepted paired S6 report |
| AC2 · Second-operator install and use | [S1 package qualification](../e10-s1-release/README.md), [S2 EC2 qualification](../e10-s2-ec2/README.md) | `install`, `alert`, `manual`: named walkthrough, exact installed IDs, HTTPS and isolation, both incident/run journeys | S7 deployment, sender integration and walkthrough |
| AC3 · Recovery trials | [S3 scenario and reset qualification](../e10-s3/README.md) | `measurement`, `recovery`: five independent S6 slots per scenario, RCA versus 12/15, fresh decisions and post-checks, plus separate S7 reproduction | Counted S6 results and S7 deployed reproduction |
| AC4 · Observations and negatives | [S3 catalog](../../../config/lab/release-scenarios-v1.json), [S6 control rules](../../evaluation-baseline.md#s6-observation-and-negative-controls) | `measurement`, `recovery`: four observations against 4/4, five negatives against 5/5, hostile/clean comparison, plus S7 diagnostic-only result | Installed S6 control receipts and S7 diagnostic-only reproduction |
| AC5 · Safety and manual outcomes | [E07 local qualification](../../e07-local-acceptance.md), [S5 restore safety](../e10-s5-restore/README.md) | `measurement`, `recovery`, `branches`: independent write reconciliation, role/race/expiry/stale/capacity/controller/restart/ambiguous-response/unsafe-inverse guards and manual paths | S6 guard and mutation receipts, deployed workflow and affected guard checks |
| AC6 · Timing and outcomes | [S4 timing contract](../../evaluation-baseline.md#timing), [S6 timing tooling](../e10-s6/README.md) | `measurement`: seven stages, availability, counts, medians/ranges, aborts, action and separate incident disposition | Accepted S6 timing and usage |
| AC7 · Model, KRR and installed UI | [E08 bounded KRR](../e08-r6-integrated.md), [E09 installed continuity](../e09-integrated.md) | `model`, `krr`, `edge`, `continuity`, `branches`: exact operational qualification, short receipt and hashes, installed UI and saved reads | Exact model, KRR driver binding and deployed short run, Edge and workflow evidence |
| AC8 · Backup, restore and safe stop | [S5 local restore receipts](../e10-s5-restore/README.md), [S2 cloud receipts](../e10-s2-ec2/README.md) | `restore`, `stop`: cutoff and loss, retained evidence, quarantine and no replay, measured footprint/headroom, dated and actual costs, and stopped inventory and ownership | Second-operator disposable writer restore, cloud stop inventory and repeated measurements where the S2 binding differs |

## Packet inventory

| Item | Available now | Attach or confirm for the selected candidate |
| --- | --- | --- |
| Bundle and locks | [Package and verification](../../release-package.md), [web lock](../../../apps/web/package-lock.json), [backend lock](../../../services/backend/uv.lock), [EC2 provider lock](../../../deploy/ec2/.terraform.lock.hcl), [contract tool lock](../../../tools/contracts/package-lock.json) | Trusted manifest, archive hashes, loaded image IDs, upstream references and fresh dependency advisory disposition |
| Setup and operations | [EC2 deploy](../../../deploy/ec2/README.md), [second-operator walkthrough](../../release-second-operator.md), [restore guide](../../release-restore.md), [job dispatch](../../job-dispatch.md) | Exact topology and host timebox, credentials passed out of band, stop and retention receipt |
| User and API guides | [First interactive journey](../../../README.md#first-interactive-journey), [API contract](../../../contracts/openapi.json), [interface ownership](../../foundation.md#interfaces-and-module-ownership) | Reviewed guide links and deployed user journeys on the selected build |
| Runbooks and scenarios | [Runbook corpus](../../runbook-corpus.md), [S3 reset receipts](../e10-s3/README.md), [triage demo](../../triage-demo.md) | Exact runbook generation and saved citations, chosen scenario IDs, qualifications and reset receipts |
| Measurements and limitations | [S6 handoff format](../../evaluation-baseline.md#s6-recovery-evidence-handoff), [S6 preparation ledger](../e10-s6/README.md) | Final scorecard, binding cross-check, usage, write reconciliation and explicit remaining limitations |
| References and attribution | [System tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3), [EPIC decisions](https://github.com/pho-veteran/cloudops-copilot/issues/13), linked feature ledgers | Verify cited revisions and guide links, package attribution and applicable notices, dependency advisory dispositions and evidence owner roles |
| Demo and release disposition | [Demo script](../../release-second-operator.md#demo-script) | Sanitized demo recording, final AC verdicts, readiness decision and separate publication authorization |

## Preparation checks

The [local preparation receipt](preparation.json) records the tested bundle and
runtime results on 2026-11-26. The build and installation shared a local Docker
engine. Deployed behavior stays open until the corresponding EC2 receipts
are attached.

| Check | Observed result |
| --- | --- |
| Compose render on Docker Compose 2.40.3 | Core, scoped release, writer restore and core-only restore file orders rendered with placeholder image references. Config checks verified selected image references, `linux/amd64`, `pull_policy: never`, the loopback-only HTTPS listener, core versus scoped collection modes and separate restore/test database names. The rendered backend services retain some `build` entries on this Compose version. Use the documented `--no-build --pull never` at startup and compare running image IDs to the manifest. |
| Bundle and core HTTPS | The source and image archives were built, verified, extracted and loaded. The fresh local project launched with `--no-build --pull never`; all five container image IDs matched the manifest and readiness returned `available`. The HTTPS probe passed secure session, Origin/CSRF, Operator admission, Viewer read/denial and old-session replay checks. |
| Core-only restore and stop | A separate `cloudops_restore_test` project restored to the recorded cutoff. 42 of 45 tables were identical, the restored session returned 401 and saved reads returned 200. The evidence digest map was unchanged for 12 metadata rows, all with unavailable payloads in core mode. The cutoff job stayed queued while the worker heartbeat advanced under recovery; it ran only after dispatch returned to normal. Both projects stopped and retained four volumes each. |
| S3 scenario CLI | Catalog validation passed for 12 scenarios (three recovery, four Pending/ImagePull and five negatives). Projection succeeded for both suggested S7 cases. That checks the catalog inputs only. |
| Focused tooling tests | The S5 restore and S3 recovery lab unit selections passed 37/37 in the bundle backend image on Linux. Native Windows passed 14 with 23 POSIX-only skips. |
| Alertmanager file readability | A no-network permission probe on the pinned image confirmed the default `65534:65534` user could not read a private operator-owned bind. It read a staged dummy `0400` file in a sender-owned volume mounted read-only. Actual sender/TLS/intake delivery on the candidate remains open. |
| Local npm dependency audit | The locked tree reported four moderate dependency entries tied to one advisory on `minimatch` through `shadcn` → `ts-morph` → `@ts-morph/common` → `minimatch`. The `--omit=dev` scan reported zero entries. The built HTTPS image held compiled web output with no `/srv/node_modules` or npm. The selected release still needs the full candidate-specific Python, npm and image advisory review. |

## Resume when the remaining inputs arrive

1. **Before #125 closes**, rehearse the v2 owned recovery lab and settle the KRR
   driver's project binding. On the approved EC2 target and selected bundle,
   collect `install`, the model-independent portions of the workflow, `edge`,
   `restore` and `stop` as their inputs become available. Use a separate
   disposable writer project for the restore. Keep rehearsal receipts with
   their own evidence class.
2. **When #125 merges**, follow the closing PR to its final evidence and bind it
   to the S7 candidate. Compare S6 and S7 source/bundle, installed images,
   workload UID, source and scope fingerprint, model CallIdentity, and the
   E07 policy and profile. Record differences, affected receipt keys and the
   check that resolves each difference. Re-run affected production-path proof.
3. **Import `measurement`** from the accepted S6 ledger. Link its seal/cohort,
   accepted revision, scorecard, immutable bundle fingerprints, operational
   dispositions for all fifteen planned slots, four observation and five negative
   receipts with evidence classes, hostile/clean result, guard results, separate
   mutation reconciliation, timing availability and actual provider
   request/token/spend/no-paid-fallback receipts. Preserve blocked, failed,
   interrupted and abstained results in their original denominators.
4. Finish the exact deployed model, KRR and reproduction receipts. Use one S7
   recovery and one diagnostic-only case. Retain both outside the campaign
   denominator unless the S6 seal predeclared them.
5. Review every acceptance row, finalize the recorded limitations and attach the
   demo recording. Set packet readiness only after the evidence review. Record
   publication authorization as a separate decision with its owner and date.

Keep raw provider context, credential files, cookies, private Cloud state, database
dumps and session recordings containing secrets outside the public packet.
