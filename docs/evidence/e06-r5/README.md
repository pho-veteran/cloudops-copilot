# E06 finite campaign acceptance

The qualified primary is `gpt-6.1-sol` on the owner-authorized local gateway, with `e06.rca_prompt/v3`, `max_completion_tokens=1024`, a 32KiB input bound and the exact identity in [rubric.json](rubric.json). `codex/gpt-6.1-terra` is compatibility-only: its actual fixed probe returned served model `gpt-6.1-terra`, but it has no diagnostic qualification. No provider credential is published or enabled by default.

The [frozen inputs](../../../services/backend/tests/fixtures/e06/cases.json) and [independent oracle](../../../services/backend/tests/fixtures/e06/oracle.json) remain unchanged from the first cohort. Expected labels never enter model context. [Instructions](../../diagnosis-acceptance.md) explain reproducible admission, egress, finite budgets, qualification and smoke.

## Results and retained failures

| Phase | Actual model requests | Result |
| --- | ---: | --- |
| Original Kiro attempt | 1 | HTTP400; unsupported gateway integration; usage/cost unknown; [retained](live-initial.json) |
| Sol and Terra fixed probes | 2 | Both HTTP200, exact `OK` marker, observed usage72 each; [receipts](gpt-probes.json) |
| Sol prompt v1 | 2 | c01 plus one repair rejected for invented schema; no result/qualification; [retained](gpt-initial.json) |
| Sol prompt v2 positives | 10 | 9/10 validated; c05 rejected for ruled-out citation coverage; usage stop prevented both negative sends; [retained](gpt-v2-initial.json) |
| Sol v2 uncalled negatives | 2 | Injection passed; healthy abstained semantically but failed structural rules. v2 remains unqualified; [retained](gpt-v2-continuation.json) |
| Sol prompt v3 complete cohort | 12 | 10/10 supported positives, 2/2 each family, 4/4 Pending/ImagePull manual-only, validated healthy abstention and safe injection; [rubric](rubric.json) |
| Fresh v3 operational smoke | 1 | Completed on a new operational Run after exact qualification; evaluation output was never adopted; [ledger](gpt-v3.json) |
| **Engagement total** | **30/50** | **151,036 observed tokens as a lower bound; one original request has unavailable usage/cost** |

Only the complete v3 cohort establishes qualification. Earlier outcomes are not pooled into its quality score. All five negatives/controls pass their declared safe behavior: actual healthy/injection inference, zero-send required-gap/foreign-workload boundaries, and controlled quota/auth/unavailable worker checks. The global observed-token stop was raised to170,000 for the bounded v3 correction; the single repair remained spent throughout. No additional model calls are needed.

## Installed acceptance

[API evidence](installed-api.json) confirms the exact fresh operational result, evaluation exclusion, comparison isolation, Viewer read/403, actual resolve/reopen/export and unchanged saved handoff/result after API restart. The saved handoff has14,889 characters and matching API/Operator/Viewer DOM SHA256. [Connected Edge evidence](installed-ui.json) covers qualified Connections, selected findings, focused exact log citation, saved history, Viewer access and responsive layouts. Temporary viewport overrides were reset; unrelated tabs were preserved.

| E06 criterion | Evidence and observed result |
| --- | --- |
| AC1 compatibility/configuration | Two distinct actual configs through the same bounded client; [probes](gpt-probes.json), [v3 calls/identity/nullable usage](gpt-v3.json), retained failure and [R1 wire/bounds](../e06-r1-model-client/README.md). Quality is separate from compatibility. |
| AC2 exact source-bound findings | [Semantic/citation adjudication](rubric.json), [actual stored citation/UI](installed-ui.json), API restart/frozen bytes and [R2 invalid-reference controls](../e06-r2-context/invalid-cases.json). |
| AC3 five families/abstention | 10/10 positives, every family2/2, manual4/4 and validated healthy insufficiency in [v3](gpt-v3.json); bounded candidates remain advice only. |
| AC4 durable bounded recovery | [R3 process/state evidence](../e06-r3-lifecycle/acceptance.json), [actual retained dump/restore drill](../e06-r3-lifecycle/restore.json), current worker/budget regressions and installed restart. No new dump/restore drill is claimed. |
| AC5 egress/untrusted input | Same context/egress/validator, actual injected/clean pair with unchanged scope/authority, [R2 denials](../e06-r2-context/egress-denials.json) and [finite controls](offline-controls.json). |
| AC6 installed/degraded journeys | Fresh live-model [API](installed-api.json)/[Edge](installed-ui.json) journeys plus [R4 unchanged component fingerprints and quota/session/Knowledge/history matrix](../e06-r4-ui/acceptance.json); current generated-contract and HTTPS CI. |
| AC7 reproducible diagnostic handoff | Frozen oracle, retained failing cohorts, exact qualified tuple, per-call usage/timing/digests, [setup and limits](../../diagnosis-acceptance.md) and consumer boundaries below. |

Focused checks:12 controlled driver/lifecycle cases,81 context/advice unit cases,811 unit/context/validation/lifecycle cases;230 web tests and production build. Required whole-repository backend/migration/contracts/HTTPS and web CI is reported against the final PR head. Old R3/R4 receipts retain their actual source fingerprints: R4 component hashes are unchanged; restore-path differences are the R4 lazy advice import and the versioned prompt correction, not a fresh restore execution.

## Consumer and release boundaries

E07 receives stored bounded advice only; fresh applicability/parameters, approval, execution and verification remain its responsibility. E09 may consume immutable observations and source references without interpreting a candidate as an executed action. E10 retains the rules comparator, independently reset live sample-app/load/recovery runs and cloud/release qualification.

This is real model inference over synthetic retained telemetry, not proof of live cluster causality, recovery success, population accuracy or an SLO. One complete bounded trial per v3 case is reported. Default deployment remains unconfigured; changing any qualified identity component requires new qualification. Billing and unknown-response cost remain unavailable where the provider supplied no usage.
