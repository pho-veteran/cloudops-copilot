# Diagnosis engine: dataset v2 design

Part of the [engine improvement loop](README.md). Status: **(to build)**, except the
difficulty audit, which exists (`engine-loop.py audit`).

The decisions D1-D5, the acceptance check, the sizes, dev-check and the host factor
below are pre-registered (2026-11-27). They were fixed before any `lab-v4` data existed
and do not change after it; see [loop v2](README.md#loop-v2).

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
- Collection runs on two owned hosts in parallel with zero model calls: the laptop
  engine lab (12 CPU, 14 GiB) and one disposable EC2 engine lab (`deploy/ec2`,
  `lab_name = "cloudops-engine-lab-v2"`). Replays use the existing cache and model call
  policy.
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
| `host` | `laptop` or `ec2`: the lab that collected the trial (environment-shift factor) |
| `log_profile`, `untrusted` | Sample application log profile, and the untrusted-text claim if any |
| `dev_check` | `true` for the fixed dev-check subset |

## Scenario catalog

Each family gets several mechanisms with different decisive cues, and each family has
look-alike scenarios whose expected label is a different family or `unknown`.
Mechanisms marked *(exists)* run today.

### Positives

| Family | Mechanisms on this lab | Decisive cue |
| --- | --- | --- |
| `crashloop_startup` | Exit 1 at startup *(exists)*; crash after serving for N s; missing required setting with a startup error line; liveness probe on a wrong path (D1), once with a clean SIGTERM exit and once with exit 137 | Non-zero exit and startup error in previous log; for the probe: `Unhealthy` liveness events followed by `Killing` with a clean previous log |
| `oom_restarts` | Limit far below the working set *(exists)*; limit at 1.1x the working set (intermittent kills); slow leak that reaches the limit late in the window | `OOMKilled` termination, memory working set reaching the limit |
| `traffic_latency_errors` | Slow handler *(exists)*; a configurable fraction of 503 responses; CPU limit low enough to throttle under the bounded load; offered load above capacity | Error ratio or p95 above threshold with no restart cue |
| `pending_unschedulable` | CPU request above node capacity *(exists)*; memory request above node capacity; `nodeSelector` with no matching node | `FailedScheduling` with different messages |
| `image_pull` | Unreachable registry *(exists)*; invalid image reference (`InvalidImageName`); `imagePullPolicy: Never` with an absent image (`ErrImageNeverPull`) | Waiting reason and pull events with different texts |

### Look-alikes (expected `unknown` unless stated)

| Looks like | Scenario | Why it is hard |
| --- | --- | --- |
| `crashloop_startup` | Missing ConfigMap reference (`CreateContainerConfigError`, decision D2) | Pod never starts, no restarts, startup-like failure |
| `oom_restarts` | Memory steady at 85-95% of the limit, no kill | Memory near limit without the decisive termination |
| `oom_restarts` | Liveness probe kill with exit 137 (pairs with OOM; expected `crashloop_startup` by D1) | Same exit code, termination reason `Error` |
| `pending_unschedulable` | `ResourceQuota` exceeded (`FailedCreate` on the ReplicaSet, no new Pod, decision D3) | Rollout stalls, but nothing is scheduled |
| `traffic_latency_errors` | Readiness probe on a wrong path: Pods Running but not Ready | Request rate drops, the load generator sees errors, the app does not |
| `traffic_latency_errors` | Load ramp within capacity | Request rate rises, latency and errors stay normal |
| any | Deployment scaled to 0 | No Pod, no failure event |
| any | Benign rollout (annotation or harmless env change) with a transient readiness warning | Breaks the rollout shortcut |

### Red herrings (collected, never derived)

A red herring is a real failure signature that is not the cause for the requested scope
and window. Each carries `distractor_sources`.

- The target had a fault that healed before the window: one OOM kill or one crash in
  the same Pod, then healthy serving. The restart counter is raised but flat, the
  last termination and its events are dated before the window.
- Untrusted text and periodic unrelated warnings in the logs (see below).
- A second true fault family on the target is out of scope for v2 (multi-fault scoring
  needs a product decision).

Dropped before collection, from the collector code: an unrelated Deployment (history)
faulted while the target is healthy, and an image pull error that resolved before the
window. The collector scopes state, events and logs to the target Deployment, its
ReplicaSets and its current Pods (`kubernetes_collector.collect_kubernetes_events`), so
neither herring can reach a bundle and both would only add healthy cases. The PVC
pending mechanism is dropped as well: it needs a second owned object and template
volume fields, and three pending mechanisms already give different
`FailedScheduling` messages.

The generator must confirm that the herring reached the bundle. A herring that the
collector filters out (for example events of another object) gives an ordinary healthy
case, and the case is tagged `herring_absent`.

### Intensity and onset ladder

Each parameterized mechanism runs at two in-distribution levels: strong, and near the
detector threshold (for example a 503 fraction of 50% and 8% against the 5% error-ratio
threshold; an allocation of 4x and 1.15x the memory limit). One further level, `mid`,
is collected for the out-of-distribution test stratum only. Onset varies between early
and late in the window, and before the window for the healed herrings. The
observability check decides the expected label at the weak end, so the ladder produces
both positives and principled abstentions.

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
  raise the memory limit" during a pending fault), not commands. Expected labels do not
  change. The scorer reports the family flip rate (`INJECTION_FLIP`) and the rate of
  citing a log segment that carries the claim on an abstention case.
- Lab-only flags are Deployment environment values of the sample application. They are
  never HTTP controls of the product, and the request field only changes what the
  sample application logs.

### No second application (D5)

There is no second application. Large demo systems do not fit next to the lab stack
(about 6 GiB available on the laptop while the lab runs). The out-of-distribution test
stratum instead holds out one log profile (`kv`) and one intensity level (`mid`). The
missing second application is reported as a limitation.

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
  units are ordered by `sha256(seed)`, and alternate units go to test and dev, the
  first to test. A unit is a trial, or a contrast pair when the trial has a
  `contrast_group`, so both members of a pair are always in the same split. This keeps
  rare cells in both splits and gives test at least 175 trials.
- Every trial with a `holdout` (log profile `kv` or intensity `mid`) goes to test.
  Test has two strata, in-distribution and out-of-distribution, reported separately.
- **Dev-check:** within dev, every fifth unit of each cell in the same order, at least
  30 trials and a fixed 20% of dev, forms `dev-check`. Agents see only aggregate scores
  of dev-check (`replay --split devcheck` writes no per-case output that an agent
  reads), never per-case outputs. The remaining dev trials (`dev-work`) are the
  iteration set.
- **Host factor:** the plan interleaves both hosts so each host covers every scenario
  cell; `host` is recorded in the meta. Splits depend only on the seeds, never on the
  host. Results are reported per host as an environment-shift check.
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
| B1, B4 | Current product path and current engine, as in the [M0-M4 report](reports/m0-m4.md) |

Acceptance check (D4). A snapshot becomes the working dataset only when all three hold
on dev:

- 0 id leaks and 0 content leaks in the leak scan;
- T0 family accuracy is at most 0.60;
- at least 40% of dev trials are in the hard stratum.

B2@`74d16ef` is reported as a baseline, not used as a gate. These thresholds are a check,
not a filter: cases are never removed to meet them. If a snapshot fails, the scenario
mix is changed and a new snapshot (`lab-v4b`) is built, with a new sealed test split.
At most two dataset attempts are allowed.

## Size and statistics

- Test: at least 175 trials. With a paired discordance of about 20%, an exact McNemar
  test at alpha 0.05 needs about 155-175 paired trials to detect a 10-point difference
  with power 0.8. Dev: at least 150 trials, of which a fixed 20% (at least 30 trials)
  form dev-check. About 325 trials in total.
- Per family: at least 30 positive trials across splits. Look-alikes, red herrings and
  healthy cases: at least 60 trials.
- Scope cuts: two in-distribution intensity levels per ladder, untrusted text that
  states plausible wrong diagnoses rather than commands, and no multi-fault scenarios.
- Confidence intervals use a trial-level cluster bootstrap (10,000 resamples). Wilson
  intervals stay for per-family rates. Report n in trials and in cases.
- Lab time: the `m0-a` batch took 3.35 minutes per trial (59 trials in 194 minutes).
  At that rate 325 trials take about 18 lab hours, or about 9-10 hours on two hosts.
  New scenarios with longer onsets, healed herrings or slow leaks take longer.

## Scoring additions

- Every scorecard is split by stratum (`easy`/`hard`), test holdout, mechanism and
  look-alike group.
- Contrast-pair accuracy: a pair counts as correct only when both members are correct.
- Distractor citation rate: cited entries that appear in `distractor_sources`. A
  distractor citation is a citation precision error.
- Untrusted-text flip rate and injected-line citation rate, as above.
- Results per host.
- Confidence intervals for accuracy and paired differences use the trial-level cluster
  bootstrap; McNemar tests stay exact and paired by case.

### Generator smoke findings (before collection)

The smoke ran one trial per cell (seed prefix `smoke-v4`, not part of `lab-v4`) on both
hosts. Changes made before the full run, without changing the cell table:

- `crash-setting` exited before its restarted instance could be read, so the previous
  log was always unreadable; the sample application now spends 6 s loading
  configuration before the error.
- `throttle` used a time-bounded work loop, which a CPU limit cannot slow; it now uses a
  fixed amount of computation per request (`LAB_WORK_LOOPS`).
- Traffic cells and traffic look-alikes wait longer after the rollout (130 s early,
  100 s late onset), so the new Pod serves for several 30 s scrape intervals.
- `missing-backoff` collects while the container is in `CrashLoopBackOff`. On this lab
  the previous log stayed readable, so these trials are ordinary crash positives
  collected in back-off rather than missing sources. The label follows the evidence.
- `missing-prometheus` checks that every Prometheus slot is unavailable instead of the
  traffic cue; the family matrix then labels it `unknown`.

### Attempt 1 (`lab-v4`) result

Collection on both hosts was stopped on 2026-11-27 after 71 trials (the first two
rounds of the interleaved plan, every cell covered), because the dev trials already
failed D4 by a wide margin. Snapshot `lab-v4` (manifest SHA-256 `a8a49499...`) holds
those 71 trials: 25 dev (4 of them dev-check) and 46 test, which stay sealed.

| Check on dev (25 trials) | Value | D4 |
| --- | --- | --- |
| Id and content leaks | 0 and 0 | pass |
| T0 family accuracy | 0.84 (21/25, Wilson 0.65-0.94) | fail (≤ 0.60) |
| Hard stratum | 16% (4/25) | fail (≥ 40%) |
| P0 | 0.36 | reported |
| B2 (detector unchanged since `74d16ef`) | 20/21 dev-work, 4/4 dev-check | reported |
| B1 / B4 | 7/21 and 25/25 on dev-work; 2/4 and 4/4 on dev-check | reported |

The Wilson lower bound already exceeds the 0.60 threshold, so the full table could not
pass. The cause is two fixed deployment properties, not the mechanisms: every crash and
OOM Deployment had a readiness probe, so `{BackOff, Unhealthy}` marked both families,
and traffic faults came without any Warning event. Pending and image pull cells are
solved by `FailedScheduling` and `Failed` whatever the mechanism.

### Attempt 2 (`lab-v4b`)

The second and last attempt changes the scenario mix only. Labels, the label rule,
the acceptance check, sizes, splits, dev-check and the host factor stay as
pre-registered. The seed prefix is `lab-v4b`, so the test split is new.

- **Readiness probe factor.** In seven trials of eight the Deployment has no readiness
  probe. A crash or OOM loop then shows `BackOff` without `Unhealthy`. The factor
  applies to every cell that does not need the probe, faults and abstentions alike
  (liveness, readiness-wrong, benign rollout, scale-to-zero and traffic cells keep it),
  so a missing probe never marks a fault.
- **Warm-up factor.** In seven trials of eight a traffic fault rolls out Pods that take
  15 s to become ready, so the fault comes with readiness warnings.
- **Second configuration look-alike.** A missing Secret reference
  (`CreateContainerConfigError`, labelled `unknown` by D2) joins the missing ConfigMap
  reference.
- **Mix.** More crash, OOM, traffic and configuration look-alike trials; pending and
  image pull keep at least 30 positive trials each; other look-alikes, herrings and
  healthy cases keep at least 60 trials together.

| Group | Cells and trials (`v4b` table) | Trials |
| --- | --- | ---: |
| `crashloop_startup` | startup exit 16; crash after serving 14; missing setting 14; liveness 6 + 6 | 56 |
| `oom_restarts` | allocation strong 16, near 14; slow leak 12 | 42 |
| `traffic_latency_errors` | slow 8 + 6; 503 fraction 8 + 6; throttle 8; overload 8 | 44 |
| `pending_unschedulable` | CPU 10; memory 10; node selector 8 | 28 |
| `image_pull` | unreachable registry 10; invalid reference 9; never-pull absent 9 | 28 |
| Configuration look-alikes | ConfigMap reference 20; Secret reference 18 | 38 |
| Other look-alikes | memory near limit 8; quota 6; readiness wrong path 6; load ramp 6; scaled to 0 2; benign rollout 4 | 32 |
| Herrings and healthy | healthy 10; healed OOM 4; healed crash 4; healthy with untrusted text 6 | 24 |
| Collected missing sources | Prometheus stopped 4; back-off 6; events purged 2 + 2 | 14 |
| Held out, test only | `kv` 4 per family and 4 healthy; `mid` OOM 4 and 503 fraction 4 | 32 |
| Total | | 338 |

With alternate assignment this gives 152 dev trials (31 of them dev-check) and 186 test
trials. The decision and its reason are recorded in the ledger (`exp-0012`).

Result: accepted on dev (ledger `exp-0013`): 0/0 leaks, T0 0.454, 54.6% hard. The
engine-lab reader token expires 8 hours after lab start; 131 trials collected after that
had every Kubernetes slot unavailable and were re-collected with the same seeds
(`engine-lab-cases.py --seeds`, PR #163). Restart long collections with
`renew-readers` before 8 hours. See the [loop v2 report](reports/v2-lab-v4.md).

## Delivery slices

Each slice is one PR. Slices 2-6 need no model calls.

1. Difficulty audit, leak scan and the T0 and P0 baselines over existing snapshots
   *(exists)*.
2. Opaque ids, neutral lab names, the meta fields above, stratified split assignment,
   and the new scorecard breakdowns with the cluster bootstrap *(exists: `engine_loop/splits.py`, `metrics.py`)*.
3. Sample application: crash-after, error fraction, leak, required setting, log
   profiles and the request-field log flag *(exists: `config/lab/sample_app.py`)*.
4. Lab fault mechanisms and look-alikes in `manage-local-lab.py`, each with a verified
   reset *(exists: `engine-fault`, `engine-reset`)*.
5. Generator plan v2: the scenario matrix, observability check, herring presence check
   and collected missing sources *(exists: `engine_loop/plan_v2.py`,
   `engine-lab-cases.py --plan v2`)*.
6. Snapshot `lab-v4`, run P0, T0 and B2@`74d16ef`, and apply the acceptance check.
7. Resume the loop on `lab-v4` dev and re-measure M1-M4.

## Decisions (resolved 2026-11-27)

The owner's defaults were adopted unchanged at pre-registration.

| Id | Question | Decision |
| --- | --- | --- |
| D1 | Label for a liveness probe on a wrong path (kubelet kills a healthy app) | `crashloop_startup`. Its `required_sources` include the `kubernetes.events` slot that carries the `Unhealthy`/`Killing` probe events, so citing only the application log does not pass. The restore plan applies |
| D2 | Label for `CreateContainerConfigError` | `unknown`: no product family claims configuration errors |
| D3 | Label for `ResourceQuota` `FailedCreate` | `unknown`: nothing is unschedulable. If the collector does not capture the ReplicaSet `FailedCreate` event, the observability check records that |
| D4 | Dataset acceptance on dev | 0 id leaks and 0 content leaks; T0 family accuracy ≤ 0.60; at least 40% of dev trials hard. B2@`74d16ef` is a reported baseline, not a gate. Cases are never removed to meet a gate |
| D5 | Second application | None. Hold out one log profile (`kv`) and one intensity level (`mid`) for the out-of-distribution test stratum, and report the missing second application as a limitation |

## Pre-registered plan v2

Trials per scenario cell, before collection (the generator's `v2` plan). Positive cells
are labelled by their family when the observability check passes; look-alike, herring
and healthy cells are labelled `unknown`. `x2` marks a ladder with two in-distribution
levels. Each host collects about half of every cell.

| Group | Cells and trials | Trials |
| --- | --- | ---: |
| `crashloop_startup` | startup exit 10; crash after serving 10; missing setting 10; liveness wrong path 12 (6 clean exit, 6 exit 137) | 42 |
| `oom_restarts` | allocation over limit x2 (strong 12, near 12); slow leak 10 | 34 |
| `traffic_latency_errors` | slow handler x2 (6+6); 503 fraction x2 (6+6); CPU throttle 8; overload 8 | 40 |
| `pending_unschedulable` | CPU request 12; memory request 12; node selector 10 | 34 |
| `image_pull` | unreachable registry 12; invalid reference 12; never-pull absent 10 | 34 |
| Look-alikes | `CreateContainerConfigError` 10; memory near limit 12; quota `FailedCreate` 12; readiness wrong path 6; load ramp within capacity 8; scaled to 0 6; benign rollout with a transient readiness warning 8 | 62 |
| Herrings and healthy | healthy 14; healed OOM before the window 8; healed crash before the window 8; healthy with untrusted text 8 | 38 |
| Collected missing sources | traffic fault with Prometheus stopped 6; crash collected during back-off 6; crash and pending with events purged before collection 3+3 (labels follow the state) | 18 |
| In distribution | | 302 |
| Held out, test only | log profile `kv`: 4 per family and 4 healthy (24); intensity `mid`: OOM 4 and 503 fraction 4 (8) | 32 |
| Total | | 334 |

With alternate assignment per cell this gives 151 dev trials (30 of them dev-check)
and 183 test trials (151 in distribution, 32 held out).

Factors applied across cells: the log profile (`plain` or `json` in distribution), and
untrusted text on about one positive trial in four. Contrast pairs: liveness exit 137
with strong OOM, `CreateContainerConfigError` with startup exit, memory near limit with
near OOM, quota with CPU-request pending, readiness wrong path with the slow handler,
and the load ramp with overload.
