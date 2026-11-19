# E08 R6 integrated acceptance

[Issue #98](https://github.com/pho-veteran/cloudops-copilot/issues/98) · [E08](https://github.com/pho-veteran/cloudops-copilot/issues/11) · [Contributor acceptance scope](https://github.com/pho-veteran/cloudops-copilot/issues/11#issuecomment-6956686057)

## Candidate and evidence boundaries

- Integrated application baseline `43eae2bc8333008ab8d5a27a9c0bc854e0c1289b`, tree `82b8e87090dca281fb7d797dd831ddd31534ff2d`. [Repository checks](https://github.com/pho-veteran/cloudops-copilot/actions/runs/44315451062) passed for web tests/build and backend tests/migrations/contract drift. The focused report/router run passed 79 tests on this baseline.
- The inherited R4 short receipt records the bounded real source and KRR path on its own candidate. The R5 persisted Edge receipt covers the controlled history, exact selection and responsive journeys. A separate R5 fixture verifies the raced 409 and retry on the merged export flow. [R4 short](https://github.com/pho-veteran/cloudops-copilot/issues/11#issuecomment-6967876857) · [R5 native and retry handoff](https://github.com/pho-veteran/cloudops-copilot/issues/98#issuecomment-6973321043)
- A separately requested six-hour run qualified on `855c9e173404e80d182610dc2d3caf808235e267`. It produced report `859eda31-ea78-459e-beee-07a4caf14385` from pinned KRR, with coverage of 361/362 one-minute bins, numeric memory advice and suppressed CPU advice (`cpu_rate_input_missing`). Its 9,312-byte export matched SHA-256 `b51f2c5e3e82996b245262e48b937e7b2c01c237db6b87c1856a2e62c822619f`. This is supplementary evidence with its own source binding. The diff to the R6 baseline is Diagnosis and generated contracts. The three Recommendation API paths and their 21 referenced schemas compare equal between the two revisions.

## Controlled checks

The controlled six-hour fixtures exercise numeric quantities and partial advice, identity and sample coverage, OOM and controller exclusions, and schema validation. Persistence tests exercise admission, authoritative publication, legacy cutover and immutable exports. Proof classes are recorded in [R1 contracts](e08-r1-contracts.json), [R2 source smoke](e08-r2-source-smoke.json), [R3 producer](e08-r3-producer.json), and [R5 UI](e08-r5-ui.json).

| Check on the integrated baseline | Result |
| --- | --- |
| WSL focused units for local lab, resource history and recommendation qualification | 191 passed. A native Windows launch had 120 setup errors because Python on Windows lacks the Linux lab's `fcntl` dependency. |
| Recommendation report and producer suites against a dedicated disposable PostgreSQL `cloudops_e08_test` | 39 passed, with a Starlette TestClient deprecation warning. The owned lab's persisted database was outside this test run. |
| Web report behavior and routes | 79 passed. The exact recommendation identifiers and cursors, pending export, raced 409, retry and stale results were exercised by behavior tests. |

## Owned lab preparation

Docker Desktop 29.9.2 reported 8 CPUs, 11,486,330,880 bytes and cgroup v2. The exact-baseline API, worker and observer all used backend image `sha256:24b76015eadbe008e560f03d5eceea3a0202111739be262b01e198588c911722`. The read-only KRR runner used `sha256:9aa19c639361205b031ac6c22706490200034e9919a29d5ca6e96422938e560a` with the merged credential owner, one 32 MiB tmpfs and all capabilities dropped. Reader and monitoring identities were renewed. The executor stayed stopped and writer authority inactive.

The first readiness invocation stopped on node containment. Kubelet advertised 3,145,732 KiB, 4 KiB over the 3 GiB gate. The owned lab's Kubelet memory reserve was increased and K3s was recreated with the existing state. Measured allocatable capacity became 3 CPUs and 3,056 MiB. The sample Pods restarted during the reconfiguration and were replaced under their original Deployments. No Node status was patched, and the gate and production recommendation thresholds were kept.

## Bounded live report

Readiness receipt `78b46752-4173-4838-ad7c-04ecbdd522f5` passed without admission. One real `--short` produced receipt `9e40c6d9-2bb8-48b1-9ab9-458810693cf9` with six checkpoints, exit 0 and runner cleanup in **103.75 seconds**, below the 138-second cap.

| Phase | Observed UTC interval (2026-11-19) | Load evidence |
| --- | --- | --- |
| Warm-up | 00:56:09.565–00:56:12.576 | 0 req/s |
| Idle | 00:56:21.154–00:56:36.164 | 0 req/s |
| Steady | 00:56:44.218–00:57:01.647 | 75 HTTP 200 at 5 req/s |
| Peak | 00:57:09.778–00:57:27.281 | 375 HTTP 200 at 25 req/s |

The Operator admission on saved workload `7d6be67e-f622-4285-a62b-a29087bed619` (`demo/checkout`, Deployment UID `69dcf1bc-9131-4d17-bfd6-627a7549f6db`, Pod-template digest `45e7c70c26f58f314d4997c7982e3b40d55cd1d033b9c1b778b02723a4e252fb`) produced terminal V2 report `af6de05c-dfd6-4a25-83d9-81715d17c081`. Its durable job `48b8c390-a5f5-41f0-801b-7857493520b3` had one succeeded attempt and was recorded as completed in PostgreSQL.

- Stored result `insufficient_data / resource_evidence_incomplete`, source `live_krr` and tool exit 0. Both the invocation and independent source receipt are present. The six-hour profile and query envelope (2026-11-18 18:57:00Z through 2026-11-19 00:57:43.888451Z) describe the request and actual queries. Measured coverage remained insufficient and the recipe recorded `short_harness_only=true`.
- Policy `e08-coverage-v1`, source config digest `6ce55a8691ea5e7015121f9b3db7a90421f5e856c2a784bf1535fbb890570615`, metrics attestation `6dfc9650f2d0841257347b0f3e08f8fa2e3ce18c145bae536760591ab0e1f443` and tool output SHA-256 `06ca74663537d814cc2973d50ebe754e5d68a9168c8a4bbd49a1535686fc225e`.
- The authorized HTTPS export of that selected ID was byte-identical before and after restarting the owned API and worker. The final export is **11,559 bytes**, SHA-256 `a0fbca0bc0b5ef272bf9bbdf5256ca009aea324f7f31c8047e14f9bb239f89d6`. Both processes had a new start timestamp with the same image.
- Direct HTTPS checks returned 401 for anonymous POST and 403 for Viewer, wrong Origin and missing CSRF. The denied requests kept the pre-run report and job counts at 3 each. Recommendation OpenAPI exposes list/admission, detail and export. The Viewer has inspect/export access.
- A controlled expiry of the dedicated Viewer fixture sessions returned 401 on HTTPS POST, with report and job counts remaining 4 each. The same report and job counts held after the Edge sign-in recovery.
- During a controlled stop of the owned KRR and Prometheus services, both the new short report and the retained full report remained readable by exact ID with identical HTTPS export bytes. Model status was `not_configured`; the saved short report returned 200. The controlled producer tests cover new attempts during source or tool faults.

## Persisted browser

The installed Microsoft Edge opened a separate lab tab. The lab CA was unknown to the existing profile at the direct HTTPS origin. The browser used a dedicated loopback development listener, which relayed to the same owned HTTPS app with the lab CA verified upstream. The HTTPS API tests exercised the direct origin separately.

From Workloads, a Viewer opened the exact saved full report for `demo/checkout`. The CPU card suppressed values with the missing-rate reason while the Memory card rendered 100 MiB (104,857,600 bytes). Enter opened the per-Pod disclosure and kept focus on its summary. Edge completed the 9,312-byte native download of that exact saved report. The file SHA-256 matched the full-run receipt and the previously verified API export. The current native viewport control stayed at a 400px minimum, so the 320px light and 390px dark checks are inherited from the R5 persisted Edge evidence.

The Viewer selected the new short report in the same persisted history. The exact report ID and insufficient-data explanation survived reload, Back and sign-in after a controlled expiry. Edge's browser download event completed with final filename `resource-report-af6de05c-dfd6-4a25-83d9-81715d17c081.json`. The final Windows file had no corresponding partial download, and its 11,559 bytes matched the authorized API export directly and by SHA-256. The browser-control policy blocked access to the internal Downloads page. Completion evidence comes from the browser event and final file.

## Acceptance ledger

| Criterion | Verdict | Evidence and scope |
| --- | --- | --- |
| AC1 | Pass | New short admission, persisted job/attempt, actual pinned KRR and independent source evidence for the saved UID, then exact Edge inspection and download. Controlled R3 numeric producer and R5 partial display evidence complement the honest short outcome. |
| AC2 | Pass | New short timeline and query envelope above, plus the R2 controlled six-hour per-Pod/container/resource tests for distinct bins, edges, gaps, freshness and foreign UID. The separate full run has its own candidate and live coverage. |
| AC3 | Pass | The short report suppresses advice in the saved payload and UI. The full report preserves the CPU missing-rate reason with eligible memory. Focused qualification and report tests cover missing replicas, true zero, absent/ambiguous/nonfinite inputs and zero restart metadata. |
| AC4 | Pass | Controlled qualification tests cover recent OOM/restart, independent CPU eligibility, HPA/VPA, field ownership and failed discovery. R3 binds those outcomes to scoped producer evidence. |
| AC5 | Pass | Direct HTTPS denial/status and counts, Viewer export and Edge session recovery. R1/R3 controlled scope and sanitization tests plus the Recommendation OpenAPI surface constrain the system to advisory report operations. The read-only lab kept its executor stopped. |
| AC6 | Pass | Exact short bytes survived restart, source stop and the native download. The earlier full report stayed byte-identical after the short report was created. Controlled R1 and PostgreSQL checks cover legacy cutover, pending export, identity replacement and applicability drift. |
| AC7 | Pass | PostgreSQL tests cover duplicate admission, pause and lease/publication fencing. R1/R3 and green repository checks cover bounded parser/runner faults, dispatch revocation and authoritative attempts. Read-only producer retry may repeat work. |
| AC8 | Pass | R5 has 76-report persisted history and separate same-name UID scopes. PostgreSQL tests cover historical reads and stale precedence; the new owned source stop preserved both exact saved exports with the model unconfigured. |
| AC9 | Pass | The actual new report was selected by ID in Edge, restored after reload/Back/expiry and downloaded byte-for-byte. R5 provides persisted active/inactive, invalid-cursor/missing-report recovery, history and narrow light/dark evidence. Pending and raced 409 behavior is covered by the earlier persisted and HTTP-fixture checks. |
| AC10 | Pass | Final application baseline, exact CI link, focused and full suite scope, image/config/timing bindings, cleanup and E09/E10 handoff are recorded here. |

## Cleanup

The ownership-guarded stop left 0 running project containers, 0 containers for the short cohort load and all 12 project volumes retained. Dispatch entered recovery and the executor remained stopped. The Edge fixture was signed out and its temporary credential was rotated; the loopback listener was stopped. Both reports, private receipts and the matching native files were retained without publishing raw report content.

## Release handoff

- [E09](https://github.com/pho-veteran/cloudops-copilot/issues/12) can display the report service projection (`component`, `status`, `observed_at`, `last_success_at`, `reason`, `source_kind`) separately from the stored result and its applicability. `resource_evidence_incomplete` describes a result with missing evidence. A saved export can remain readable when a source or producer is unavailable.
- [E10](https://github.com/pho-veteran/cloudops-copilot/issues/13) should carry forward the owned-lab readiness and `--short` sequence, Linux host PID/cgroup visibility, KRR credential ownership and tmpfs requirements, the 138-second post-preflight cap, explicit report/export binding, and stop-with-retention checks. A separately scheduled full cohort also depends on stable host power and clocks. The first full attempt recorded a failed checkpoint after Modern Standby shifted the phase clock against the wall deadline. The completed rerun kept the host awake throughout.
