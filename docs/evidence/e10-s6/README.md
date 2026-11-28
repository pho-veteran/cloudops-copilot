# S6 local verification — November 24, 2026

Campaign results are recorded separately in
[R3 S6 original campaign results](r3-20261125/README.md),
[R4 S6 live campaign results](r4-20261128/README.md) and
[R5 S6 live campaign results](r5-20261128/README.md). S6 remains open;
the historical candidate-specific evidence below is preserved unchanged.

Issue #125 remains incomplete. This checks the uncommitted candidate based on
`6ac5bf92d44559066a677bdc3efe1f01656cc473`. The source hashes, measured calibration,
qualification binding and result counts are in [verification.json](verification.json).
The existing S3/S4/S5 receipts do not establish S6 acceptance for this candidate.

## Implemented candidate

The candidate contains cohort freezing, immutable context capture and trial/reset
bindings, finite workers with durable request/token/spend reservations, product
proposal/approval/evidence probes, and a scorecard preserving the fifteen planned
recovery slots. Worker changes support selecting exact job kinds and subjects.
Controlled tests cover capture/rebinding, limits, source drift and denominator rules.
These are implementation and controlled verification results.

## Checks performed

| Check | Observed result |
| --- | --- |
| Ruff check / format / whitespace | Passed; 372 Python files already formatted |
| Focused S6/S3 and diagnosis lifecycle on Linux/PostgreSQL | 90 passed |
| All backend unit tests plus selected jobs, dispatch, diagnosis and recovery PostgreSQL regressions | 1,407 passed, 1 failed, 1 skipped; 358 seconds |
| Skipped Compose rendering check, separately on WSL with Docker CLI | 1 passed |
| Isolated inverse group | 24 passed, 1 failed |
| Inverse group with timestamp-only diagnostic instrumentation | 25 passed |
| Authenticated provider protocol probe | HTTP 200, exact `gpt-6.1-sol`, 27 observed tokens |
| Source-built owned K3s/HTTPS lab and recovery profile loading | Started; profiles loaded by API |
| Live traffic-scale desired-state calibration | Passed, 5 samples at a fixed offered 30 rps; returned to 1 replica |
| Current-checkout recheck on Windows | Ruff check/format passed; S6 unit tests 23 passed, 1 skipped. `scripts/e10_campaign.py` changed after the archived Linux snapshot; the archived Linux/PostgreSQL counts apply to that snapshot. |

The broader PostgreSQL regression selection covered `test_jobs`, `test_dispatch_start`,
`test_dispatch_gate`, `test_diagnosis_lifecycle`, `test_diagnosis_processes`, and recovery
`races`, `lifecycle`, `plans`, `inverse`, `processes`, `verification`, `gateway_v2` and
`restore`. It used only a dedicated `cloudops_test` database. It was not the entire
backend integration suite. JUnit files are retained privately under
`tmp/s6-verification-20261124/`.

The initial native Windows run was unsuitable for the Linux lab tests (`fcntl` and
POSIX behavior). A Windows-bind-mounted Linux regression run was interrupted after
showing a failure and poor filesystem performance. The complete counts above come
from a source snapshot copied into the Linux filesystem, with the original failures
retained. A passing diagnostic rerun does not erase them.

## Unresolved test failure

The complete Linux suite failed
`test_fresh_inverse_boundaries_deny_before_approval[fields]` during forward plan
setup: HTTP 409 `target_cooldown` instead of 201. The isolated inverse rerun failed
`test_captured_template_inverse_needs_no_new_forward_selector[set_memory_profile]`
during forward approval: the same code, instead of HTTP 200. The timestamp-only
diagnostic rerun passed all 25. The root cause remains unresolved; the suite and the
installed unsafe-inverse acceptance are not declared passed. No cooldown guard was
weakened or bypassed.

## Live lab findings

The lab is the owned `cloudops-e10-s3-lab`, with its source-built runtime and exact
UID/profile bindings recorded in the JSON. The manager runs in a helper with the
Docker engine's PID/cgroup namespaces, as required for containment proof. The helper
uses a separate tooling project label; lab ownership labels were preserved.

The first calibration was refused before measurement because Node allocatable memory
was 3 GiB plus 4 KiB. Increasing the private generated kubelet system reserve by
16 MiB and using the supported quarantined recreate brought it below the 3 GiB bound.
No Node capacity field or containment threshold was changed. The standalone verify
attempt then refused an unwarmed metric window; the subsequent calibration generated
and measured real load.

Traffic calibration passed all five samples: served rate 28.70–29.39 rps, worst error
ratio 0.00385 and worst p95 0.244 seconds. Every sample had two serving Pods and zero
counter resets. Its load was removed and the original one-replica baseline restored.
This is trusted operator calibration of the desired action state, not an Investigator
recovery trial, product approval/apply/post-check result or S6 score.

The API's installed diagnosis gate is `not_configured`. The approved host endpoint
and credential were checked with a single product-transport probe, but there is no
new exact operational model qualification for this installed lab. The latest approved
engagement ceiling is 35 requests, 2,329,600 reserved tokens and USD 7, with no paid
fallback. One request has been attempted, using 27 observed tokens. Actual charges
and gateway-level no-paid-fallback behavior remain unverified. No further inference
was sent and no provider qualification was fabricated.

## Remaining acceptance

- **AC1/AC3:** 0 of 15 qualifying live recovery trials recorded. There is no paired
  live RCA score, recovery success rate or 12/15 verdict.
- **AC4:** The four observations, five negatives and hostile/clean comparison have
  not been measured as an installed S6 campaign.
- **AC5:** Installed role/race/expiry/stale/controller/capacity/restart/inverse evidence
  and independent cluster mutation reconciliation remain outstanding. Zero uncontrolled
  writes is unverified.
- **AC6/S6 usage:** Per-trial phases, action and independent incident outcomes, paired
  statistics, actual charges and no-paid-fallback receipts remain outstanding.

Before measured faults, finish exact operational provider qualification/configuration,
the OOM and known-good-restore calibrations and baseline, the frozen measured cohort,
and run-specific backup/abort and independent mutation capture. Preserve the original
trial denominators and all failures. The S6 issue and release acceptance remain open.

The lab was retained for inspection. Recovery dispatch was closed and the
final writer-quarantine receipt confirms `enabled=false`, `recovery_mode=true` for
the exact target UID. The separate test runner and test database containers were
stopped after retaining JUnit evidence. At verification time the candidate was
uncommitted; this handoff PR commits the same source without adding a live trial.

## Additions in the replacement PR — November 25, 2026

The replacement PR keeps the contributor commit above and adds tooling only. It adds no
live trial, provider request or control result.

- `scripts/e10_campaign_timing.py` derives the seven S4 stage durations from the S3
  trial phases and the product Incident, Run, Analysis, plan, execution and
  verification views. `s6-evidence` now also saves the Run and Analysis views.
- The four observations and five negatives follow the catalog's controlled-fixture
  route. The E06 driver can keep the prepared context each send used. Then
  `scripts/e10_campaign_controls.py` publishes those contexts as the planned bundles
  and derives every control outcome from scored receipts, the driver ledger or a
  JUnit report named after the sealed revision.
- A retained context's digest is recomputed from its content before a bundle is
  published. A configured driver run is bound to a digest of the backend source.
  The scorecard accepts a control only when its artifact states the same outcome.
- The bundle validator compared runbook chunk bytes with the whole document's hash. A
  chunk from a multi-section runbook was therefore rejected. It now checks the chunk
  hash and keeps the document hash as metadata.

The full Linux/PostgreSQL backend suite passed on the replacement head; the counts are
in the PR. The unresolved inverse-test failures above did not recur in that run. That
run does not establish a root cause, so the failures remain open.
