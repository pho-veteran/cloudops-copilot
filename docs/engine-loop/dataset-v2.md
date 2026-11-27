# Diagnosis engine: dataset v2 design

Part of the [engine improvement loop](README.md). Status: **(to build)**, except the
difficulty audit, which exists (`engine-loop.py audit`).

Dataset v2 replaces `lab-v3` as the loop's working dataset. Its first snapshot is
`lab-v4`. `lab-v3` stays frozen as a regression set, and the
[M0-M4 report](reports/m0-m4.md) keeps its identities.

## Why `lab-v3` cannot drive the loop

`lab-v3` measured that the pipeline works on this lab. It cannot rank engine versions:

| Finding on `lab-v3` | Evidence |
| --- | --- |
| Saturated | B2 (detector only) and B4 score 85/85 on dev and 35/35 on test. Every B4 ablation also scores 85/85 |
| A shortcut rule solves dev | The T0 signature rule below reads three bundle features (Warning event reasons, an `OOMKilled` termination, the maximum error ratio or p95) and the product family matrix. It scores 85/85 on dev with no log text, signal table or model. On the E06 fixtures, whose events carry no `type`, it scores 3/12 |
| One signature per family | Every dev crashloop and OOM case has exactly the Warning reasons `{BackOff, Unhealthy}`, every image pull `{Failed}`, every pending `{FailedScheduling}`. Every traffic positive has no Warning event. The OOM/crashloop split is the termination reason alone |
| A rollout marks every fault | Every injection changes the Deployment template, so 48 of 52 dev positives carry a rollout signal (`ReplicaSetUpdated` or `ScalingReplicaSet`). None of the 18 healthy, healthy-derived and out-of-scope negatives does |
| Few independent trials | 85 dev cases come from 58 trials. Derived variants copy their parent's evidence, so the effective sample is closer to the trial count |
| Variation is synthetic | Log noise, instruction text and disabled sources are derived copies. The sample application logs only fixed startup lines and nothing per request |
| Names carry the answer | Case ids and `source.variant` encode the fault kind. Model requests do not contain them (0 of 572 cached requests), but agents that iterate on the dev split read them. The unreachable image is named `cloudops-unavailable` (9 dev bundles) and synthetic events are named `engine-noise-*` (7 dev bundles). Both strings reach the model |
| Init-retry tests nothing | All init-retry trials are ineligible (empty current log), so the kind adds abstentions only |

## Goals and constraints

- The product family set and family matrix do not change: five families plus
  `unknown`. A case whose true cause is outside the five families is labelled `unknown`
  (abstain).
- The dataset must rank engine versions. A change that helps must be able to show a
  10-point paired difference on test.
- Collection stays on the single owned laptop lab (12 CPU, 14 GiB on the current
  host) with zero model calls. Replays use the existing cache and model call policy.
- Labels stay outside bundles. The test split stays sealed.
- Product, executor, approval and recovery code are not changed. Lab tooling, the
  sample application, the generator, the scorer and the engine-loop CLI may change.

## Label model

Each case records two things separately:

- **Injected**: what the generator did (scenario, mechanism, intensity, onset,
  target). This lives in the case meta file only.
- **Expected**: the label the evidence supports, from the labelling rule of the
  scenario and an observability check on the collected bundle.

The observability check runs after collection and before the case is written. It
confirms that the decisive cue of the scenario is present in the bundle (for example
an `OOMKilled` termination inside the window, a `FailedScheduling` event for the target
Pod, or an error ratio above the detector threshold for at least two samples). When the
cue is missing, the case is relabelled `unknown` and tagged `silent`, or discarded when
the scenario requires the cue. The check is deterministic and recorded in the meta file,
so a reviewer can audit every relabel.

New meta fields (case meta, not `EvalLabel`):

| Field | Meaning |
| --- | --- |
| `scenario`, `mechanism`, `intensity`, `onset` | Injected factors |
| `observable` | Result of the observability check, with the matched cue |
| `contrast_group` | Id shared by the members of a minimal pair (see below) |
| `distractor_sources` | Entry refs that belong to a red herring; citing one is a citation error |
| `stratum` | `easy` or `hard`, assigned by the audit (T0 correct or not) |
| `holdout` | `none`, or the held-out factor for out-of-distribution test cases |

## Scenario catalog

Each family gets several mechanisms with different decisive cues, and each family has
look-alike scenarios whose expected label is a different family or `unknown`.
Mechanisms marked *(exists)* run today.

### Positives

| Family | Mechanisms on this lab | Decisive cue |
| --- | --- | --- |
| `crashloop_startup` | Exit 1 at startup *(exists)*; crash after serving for N s; missing required setting with a startup error line; liveness probe on a wrong path (proposed label, decision D1) | Non-zero exit and startup error in previous log; for the probe: `Unhealthy` liveness events followed by `Killing` with a clean previous log |
| `oom_restarts` | Limit far below the working set *(exists)*; limit at 1.1x the working set (intermittent kills); slow leak that reaches the limit late in the window | `OOMKilled` termination, memory working set reaching the limit |
| `traffic_latency_errors` | Slow handler *(exists)*; a configurable fraction of 503 responses; CPU limit low enough to throttle under the bounded load; offered load above capacity | Error ratio or p95 above threshold with no restart cue |
| `pending_unschedulable` | CPU request above node capacity *(exists)*; memory request above node capacity; `nodeSelector` with no matching node; PVC with a missing storage class | `FailedScheduling` with different messages |
| `image_pull` | Unreachable registry *(exists)*; invalid image reference (`InvalidImageName`); `imagePullPolicy: Never` with an absent image (`ErrImageNeverPull`) | Waiting reason and pull events with different texts |

### Look-alikes (expected `unknown` unless stated)

| Looks like | Scenario | Why it is hard |
| --- | --- | --- |
| `crashloop_startup` | Missing ConfigMap reference (`CreateContainerConfigError`, decision D2) | Pod never starts, no restarts, startup-like failure |
| `oom_restarts` | Memory steady at 85-95% of the limit, no kill | Memory near limit without the decisive termination |
| `oom_restarts` | Liveness probe kill with exit 137 (pairs with OOM) | Same exit code, termination reason `Error` |
| `pending_unschedulable` | `ResourceQuota` exceeded (`FailedCreate` on the ReplicaSet, no new Pod, decision D3) | Rollout stalls, but nothing is scheduled |
| `traffic_latency_errors` | Readiness probe on a wrong path: Pods Running but not Ready | Request rate drops, the load generator sees errors, the app does not |
| `traffic_latency_errors` | Load ramp within capacity | Request rate rises, latency and errors stay normal |
| any | Deployment scaled to 0 | No Pod, no failure event |
| any | Benign rollout (annotation or harmless env change) with a transient readiness warning | Breaks the rollout shortcut |

### Red herrings (collected, never derived)

A red herring is a real failure signature that is not the cause for the requested scope
and window. Each carries `distractor_sources`.

- An unrelated Deployment in the namespace (history) is faulted (OOM, crash or image
  pull) while the target is healthy, or while the target has a different fault.
- The target had a fault that was reset before the window: restart counter raised
  but flat, events last seen before the window.
- An image pull error that resolved before the window, followed by a healthy rollout.
- A second true fault family on the target is out of scope for v2 (multi-fault scoring
  needs a product decision).

The generator must confirm that the herring reached the bundle. A herring that the
collector filters out (for example events of another object) gives an ordinary healthy
case, and the case is tagged `herring_absent`.

### Intensity and onset ladder

Each parameterized mechanism runs at three or four levels (for example memory limit
0.2x, 1.0x and 1.1x of the working set; handler delay 50, 200 and 1000 ms; 503 fraction
2%, 10% and 50%). Onset varies: early, middle, last 15 s of the window, and before the
window (healed). The observability check decides the expected label at the weak end, so
the ladder produces both positives and principled abstentions.

### Collected missing sources

Missing sources are produced on the lab instead of derived:

- Events purged after the fault and before collection: the events slot is available but
  empty, so state must carry the answer.
- Prometheus scrape stopped for the window: traffic family ineligible, expected
  `unknown`.
- Collection after back-off: the previous log is unreadable, crashloop and OOM
  ineligible, expected `unknown`.

Derived disabled-source variants stay for coverage, but do not count toward the
statistical sample.

### Log realism and untrusted text

- The sample application gains log profiles: plain text, JSON and `key=value`, several
  templates per message, per-request info lines, unrelated periodic warnings in healthy
  mode, and different exception types for crashes. A trial picks a profile, and one
  profile is held out for test.
- Untrusted text is collected through a lab-only flag that logs one request field. The
  load generator sends plausible but wrong diagnoses in that field (for example "OOMKilled,
  raise the memory limit" during a pending fault), not only imperative instructions.
  Expected labels do not change. The scorer reports the family flip rate and the rate
  of citing an injected line.

### Optional second application

A second small service with different health, log and metric conventions gives a
held-out application for an out-of-distribution test stratum. Large demo systems do not
fit next to the lab stack on this host (about 6 GiB available while the lab runs). The
second application is optional and decided before `lab-v5` (decision D5).

## Leakage controls

- Case ids are opaque: `c-` plus 12 hex characters derived from the trial seed. The
  bundle `source.variant` becomes opaque as well. Scenario names live only in meta and
  label files.
- Lab object names are neutral: no image, event, component or label names that contain
  a scenario word (`unavailable`, `noise`, `fault`, `inject`, a fault kind).
- `engine-loop.py audit` scans every bundle for scenario tokens. A snapshot with any hit
  is rejected.

## Splits

- The unit of assignment and statistics is the trial. Derived cases inherit their
  trial's split, as today.
- Assignment is stratified: within each scenario cell (scenario, mechanism, intensity),
  trials are ordered by `sha256(seed)` and every third trial goes to test. This keeps
  rare cells in both splits.
- Test has two strata: in-distribution, and out-of-distribution (`holdout` set: one
  held-out log profile, one held-out intensity level per ladder, and the second
  application when it exists). They are reported separately.
- The audit writes each case's `stratum` to `audits/<snapshot>-<split>.json`; snapshots
  stay immutable. Slice 2 records the stratum in the case meta when a v2 snapshot is
  created. Headline numbers are reported on all cases and on the hard stratum.

## Baselines and dataset acceptance

Baselines run on every snapshot before any engine iteration:

| Id | Baseline |
| --- | --- |
| P0 | Majority class |
| T0 | Signature rule: Warning event reasons, `OOMKilled` termination, maximum error ratio or p95, abstaining when the matched family is ineligible (`engine-loop.py audit`) |
| B2@`74d16ef` | Detector v1 as merged in PR #154, frozen as a reference |

A snapshot becomes the working dataset only when, on dev:

- the leak scan has 0 hits;
- T0 family accuracy is at most 0.60, and at least 40% of trials are in the hard
  stratum;
- B2@`74d16ef` macro-F1 is at most 0.80.

These thresholds are a check, not a filter. Cases that B2 solves are never removed to
meet them. If a snapshot fails, the scenario mix is changed in the next snapshot.

## Size and statistics

- Test: at least 175 trials. With a paired discordance of about 20%, an exact McNemar
  test at alpha 0.05 needs about 155-175 paired trials to detect a 10-point difference
  with power 0.8. Dev: at least 200 trials.
- Per family: at least 30 positive trials across splits. Look-alikes, red herrings and
  healthy cases: at least 60 trials.
- Confidence intervals use a trial-level cluster bootstrap (10,000 resamples). Wilson
  intervals stay for per-family rates. Report n in trials and in cases.
- Lab time: the `m0-a` batch took 3.35 minutes per trial (59 trials in 194 minutes).
  At that rate 375 trials take about 21 hours, run as overnight batches. New scenarios
  with longer onsets or slow leaks take longer.

## Scoring additions

- Every scorecard is split by stratum (`easy`/`hard`), test holdout, mechanism and
  look-alike group.
- Contrast-pair accuracy: a pair counts as correct only when both members are correct.
- Distractor citation rate: cited entries that appear in `distractor_sources`. A
  distractor citation is a citation precision error.
- Untrusted-text flip rate and injected-line citation rate, as above.

## Delivery slices

Each slice is one PR. Slices 2-6 need no model calls.

1. Difficulty audit, leak scan and the T0 and P0 baselines over existing snapshots
   *(exists)*.
2. Opaque ids, neutral lab names, the meta fields above, stratified split assignment,
   and the new scorecard breakdowns with the cluster bootstrap.
3. Sample application: crash-after, error fraction, leak, required setting, log
   profiles and the request-field log flag.
4. Lab fault mechanisms and look-alikes in `manage-local-lab.py`, each with a verified
   reset.
5. Generator plan v2: the scenario matrix, observability check, herring presence check
   and collected missing sources.
6. Snapshot `lab-v4`, run P0, T0 and B2@`74d16ef`, and apply the acceptance check.
7. Resume the loop on `lab-v4` dev and re-measure M1-M4.

## Decisions needed

| Id | Question | Proposal |
| --- | --- | --- |
| D1 | Label for a liveness probe on a wrong path (kubelet kills a healthy app) | `crashloop_startup`, with the probe events as required sources; the restore plan applies |
| D2 | Label for `CreateContainerConfigError` | `unknown`: no product family claims configuration errors |
| D3 | Label for `ResourceQuota` `FailedCreate` | `unknown`: nothing is unschedulable |
| D4 | Dataset acceptance thresholds | As listed above |
| D5 | Second application for the out-of-distribution stratum | Decide after `lab-v4` |
