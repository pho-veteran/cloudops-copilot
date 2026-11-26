# R3 S6 original campaign results

S6 is **blocked**. All 15 planned faults, collections and independent resets were
attempted once, but only six slots have qualifying captured live provenance. Nine
slots stopped before provider send because required context exceeded the qualified
65,536-byte budget. One of the six captured slots has a required-source scoring
block. Issues #125, #126, #13 and #3 remain open. The owner's allowance for RCA below
12/15 does not waive blocked slots or hard gates.

## Binding and evidence

Operator: `MinhKhoa2209`. Candidate R3:
`2ec3a3e4eb24dad6b1d9d6af6f22652735e1b1b6` (PR #144). Environment:
`cloudops-release-recovery-r3`, installed from the verified release bundle on owned
Docker Desktop/WSL Ubuntu. Deployment UID:
`4f448529-7f2a-4c63-99d0-de172087f5ee`. Bundle manifest SHA-256:
`2185b328f8384e91478dc4a078af538a7315037e04b4ddef759417b45b4ed683`.
S3 source binding:
`60140bf4c07e0477d44898cf4d079fcc44c5bde0af47f70eaf8eafc1160ff45b`.

The [artifact manifest](artifact-manifest.json) hashes the selected machine
receipts. Raw cluster audit, executor journal, provider records, credentials and
database dumps remain private. Their hashes and checked relationships appear in
the exported receipts. These are local results; no EC2/S7 acceptance is inferred.
The [older S6 verification](../README.md) remains historical evidence for its own
candidate and has not been rewritten as R3 proof.

Before the measured cohort, all three exact-UID recovery calibrations passed.
The [qualification rubric](qualification-rubric.json) recorded 10/10 positives, 4/4 manual-only
observations, 5/5 safe controls and no invalid promotion. Its 12 requests used
47,314 observed tokens. The separate [installed smoke](smoke-receipt.json) sent
61,221 bytes, used 16,477 tokens and was independently reset. Neither calibration,
qualification, smoke nor controlled fixtures fill a recovery slot.
The [operational boundary](operational-boundary.json) records worker-only secret
isolation; the API received no model credential.
The original [qualification identity receipt](qualification.json) is byte-preserved;
its logical evidence reference predates this publication directory. The corresponding
rubric is linked here without rewriting that original receipt.

## Original outcomes

The unchanged [sealed scorecard](final-scorecard.json),
[paired scoring receipts](paired-score-receipts.json) and
[attempt summary](original-attempts-summary.json) preserve every original outcome.

| Original slots | Observed result | Scoring and recovery |
| --- | --- | --- |
| Traffic 01-04 | Four settled exact-model calls; `traffic_latency_errors`, no candidate | Category passes; runbook-reference, candidate, action and resolution dimensions fail. No action; incidents open. |
| Traffic 05 | One settled call; `scale_deployment` | Reviewed digest approved; replicas 1 to 2 only; execution and application verification succeeded. Incident explicitly resolved afterward through the lifecycle API. Runbook-reference and frozen resolution dimensions fail. |
| Memory 01 | One settled call; `oom_restarts`, `set_memory_profile` advice | Trusted writer enablement denied `maintenance_state_changed` because target was not fully ready, before proposal. No plan, approval or recovery write. Paired scores blocked because `kubernetes.logs.previous` coverage was partial. |
| Memory 02-05 | Four `insufficient_evidence / required_targets_exceed_budget`, zero calls | Original outcomes blocked; paired scores missing/input_invalid because no captured context exists. |
| Restore 01-05 | Five identical pre-send context rejections, zero calls | Original outcomes blocked; paired scores missing/input_invalid. No candidate or action. |

All 15 independent resets restored baseline with zero residual eligible actions
and zero duplicate targeted loads. For the nine uncaptured slots the campaign
schema cannot bind a reset without capture; their original reset hashes remain in
the attempt summary. No capture was invented. Interrupted/no-action S3 recovery
receipts retain their incomplete status and reason even when baseline reset passed.
Actual final incident states are 14 open and one resolved. The frozen labels expect
incident disposition `not_applicable`; their resolution scoring therefore fails
for the actual resolved/unresolved dispositions. Labels and original scores remain
unchanged, and incident state is reported separately from execution success.

Traffic-05 plan `73a0609b-5b0f-4fe4-a6ab-2783d91fbbe4`, digest
`593cd3107d88656a7066d832f094a6c53af8260e8524ce0d3429115ff52204d3`, execution
`14c1527a-f486-4f84-bc7e-bbcb194169d4` binds the sole approved product mutation.
Image/resources stayed unchanged. Verification passed five qualified application
samples at approximately 30 rps. Acknowledgement was
`2026-11-25T19:23:20.661523Z`; final verification was
`2026-11-25T19:26:21.564344Z`: 180,903 ms total, with the configured 60-second
warmup inside the full 180-second verification window.

## Scores, controls and uncertainty

| Measure | Result |
| --- | --- |
| Planned recovery denominator | 15, unchanged |
| Captured qualifying live provenance | 6/15 |
| Investigator RCA category | 5/15; descriptive 95% Wilson interval 0.1518-0.5829 |
| Rules RCA category | 0/15; descriptive 95% Wilson interval 0-0.2039 |
| Scenario category passes | Investigator traffic 5, memory 0, restore 0; rules all 0 |
| Paired report | Investigator-only 5, neither 1, missing 9 |
| Operational dispositions | 6 completed, 9 blocked; completed does not mean every scoring dimension passed |
| Investigator score dispositions | 5 scored, 1 required-source blocked, 9 missing context |
| Actual product actions | 1 succeeded; 14 not executed |
| Observation and negative controls | 4/4 and 5/5; controlled fixture route, including hostile/clean consistency |
| Controlled guards | 128 + 25 tests passed in exact R3 image; six guard artifacts bound to source/image/JUnit |

[Control bindings](control-bindings.json) keep original artifact digests. Guard
JUnit receipts are [general](guards-junit.xml) and
[approval/inverse](approval-guards-junit.xml); the four quota/unavailable tests are
[separate](quota-junit.xml). Release-image modules were byte-compared with installed
source before guard testing. These are controlled tests, including actual killed
worker processes and HTTP transports, not independent live fault campaigns.
[Installed Viewer calls](live-role.json) returned 403 for propose/approve/inverse,
with plans unchanged. Approval/inverse used nonexistent IDs and establish permission
ordering only.

The [mutation reconciliation](final-mutation-reconciliation.json) preserves the
pre-campaign audit prefix and accounts for 24 successful Deployment patches:
10 trusted fault injections, 11 trusted resets, two maintenance annotation fences,
and the approved scale. Every patch tests exact UID/resource version; product patch,
plan/digest/approval and acknowledged journal operation match once. There were zero
observed uncontrolled writes and no unknown attributions; all targets were quarantined.
Other system/controller mutations have metadata coverage under the reviewed audit
policy. Maintenance fence proof versions come from fresh GETs after controller
status updates, so they differ from patch response versions; nonce, snapshot,
timing, intervening controller events and next conditional preconditions were checked.
The initial checker failure is retained privately.

The [zero-write control](zero_uncontrolled_writes.evidence.json) and
[fallback control](no_paid_fallback.evidence.json) remain **blocked** for full-campaign
acceptance because the qualifying live population is incomplete. Observed zero wrong
writes and a single-member route do not manufacture 15 qualifying trials.

## Timing and provider accounting

The sealed scorecard uses a **lower median** over available qualifying-live timing.
Its missing counts include excluded slots; they do not say no timestamp was retained.
The attempt summary separately uses ordinary medians over all original attempts.
Analysis time for zero-call slots measures local rejection, not provider latency.

| Stage | All-attempt observed / missing | Ordinary median ms | Range ms |
| --- | --- | --- | --- |
| Inject | 15 / 0 | 40,111 | 39,192-87,324 |
| Detect | 15 / 0 | 15,093 | 14,562-19,377 |
| Collect | 15 / 0 | 7,102 | 6,995-9,260 |
| Analyze, including nine local rejections | 15 / 0 | 207 | 188-13,281 |
| Approval | 1 / 14 | 24,564 | 24,564-24,564 |
| Apply | 1 / 14 | 6,997 | 6,997-6,997 |
| Verify | 1 / 14 | 180,903 | 180,903-180,903 |

The six qualifying Investigator analysis durations have a lower median of 11,329 ms
and range 10,499-13,281 ms. Rules comparison analysis has six observations, each
1 ms. It does not acquire the product's inject/collect/approval/apply timing.
Memory-02's original missing score receipts preceded timing retention; its later
runtime timing is included only in the supplemental all-attempt summary.

The [provider accounting receipts](provider-accounting.json) retain each reservation
and settled usage receipt, with the smoke in a separate authority.
Main campaign: six reservations, six settled exact-model receipts, 83,704 observed
tokens, zero missing usage, no provider stop reason. Reserved token quantity was
491,520. The reporter's `reserved_spend_microusd` field is **491,520 nominal
reservation units, not a price or actual spending**. Estimated and billed costs are
unavailable (`null`), never zero. Gateway output limits are ignored, so requested
completion bounds do not guarantee remote token use or charges.
The [engagement budget](engagement-budget.json) retains 61/84 used requests and
23 remaining across the entire engagement, including historical attempts.

## Concrete blockers and disposition

The [post-trial read-only diagnostic](final-context-diagnostics.json) reconstructed
required serialized input under the unchanged policy: memory 02-05 needed
67,357 / 70,212 / 73,212 / 76,145 bytes; restore 01-05 needed
81,083 / 84,764 / 87,636 / 89,133 / 89,872 bytes. Required event-target counts grew
across those retained Runs. This reconstruction uses no model calls, no configuration
variants and no replacement evidence; the original terminal receipts are authoritative.
It is a sizing observation, not a reviewed context-selection fix.

Installed Knowledge status at diagnostic time was `not_indexed`, with zero retained
document versions, chunks or generations and no retrieval availability. Captured
advice retained `runbook_not_selected`; all five scored traffic cases fail the
runbook-reference dimension. No index was built or substituted mid-cohort.

The [cleanup receipt](final-cleanup.json) confirms dispatch closed, writer quarantined,
all R3 project containers and the diagnosis sidecar stopped, task relay stopped,
volumes/history/journal preserved, and a fresh private backup hashed. The owner's
Windows gateway was left running on `127.0.0.1:20128`; no AWS resource was created.

Further inference requires an owner decision: a changed identity needs fresh
qualification and smoke before a new declared cohort, while a full 12-call
qualification + one smoke + 15 slots requires at least 28 requests versus 23 remaining.
Do not raise the 84-request ceiling, omit required evidence, revise frozen labels,
replay completed sends or replace failed slots. S7 deployment and parent close-out
remain blocked behind S6. No release tag or GitHub Release is published.

Recorded scope changes/limits: direct OpenAI Luna was retired after its 429 limit;
the product now requests exact `gpt-6.1-sol` through the owner's loopback 9router
combo, whose [final read-only receipt](combo-final-native.json) has one member,
`cx/gpt-6.1-sol` (owner-reported Fallback, stored kind null). Provider billing remains
unverified. Product and review use the same model family/route. Operator independence
is not established. Captioned screenshots replace the planned video for S7, but no
new S7 screenshots or walkthrough are claimed here. Local system-Pod
[headroom](headroom-summary.json) does not close the earlier EC2 inventory/deviation
gaps. R3 dependency/image advisories retain unresolved applicability limits; this
evidence does not assert a vulnerability-free or production-qualified release.
