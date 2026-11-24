# E10 S3 scenario inputs and reset receipts

This directory documents the reusable inputs and reset protocol for E10 slice S3. The frozen case oracle is separate from Investigator/rules inputs, and operator fault/reset controls are kept in a separate fixture file. The receipt shape is the shared `E10TrialResetReceipt/v1` handoff for S4/S6.

## Frozen scenario package

- [Scenario catalog](../../../config/lab/release-scenarios-v1.json) maps the three qualified recovery scenarios, four Pending/ImagePull variants, and five fixed negatives.
- [Case inputs](../../../services/backend/tests/fixtures/e10_s3/inputs.json) adds the insufficient-evidence, out-of-scope, and provider unavailable/quota requests. Other evidence cases reference the pinned E06 fixture IDs without copying their payloads.
- [Operator controls](../../../services/backend/tests/fixtures/e10_s3/controls.json) holds fault and provider controls, outside the case input projection.
- [Sealed oracle](../../../services/backend/tests/fixtures/e10_s3/oracle.json) holds expected classifications/action mappings. Its SHA-256 is pinned in the catalog. Updating the oracle requires a new catalog version and a new frozen cohort.
- [Reset receipt schema](../../../config/lab/e10-s3-trial-reset-receipt.schema.json) defines the per-trial scope, identity, resource/headroom, source, timing, and residual-action/load fields.

Validate the pins, references, case counts, and input/oracle boundary with:

```sh
uv run --project services/backend --frozen python scripts/release_scenarios.py validate
```

Inspect the exact input projection without case IDs, source labels, operator controls, or oracle fields with:

```sh
uv run --project services/backend --frozen python scripts/release_scenarios.py project negative-untrusted-log
```

The case resolver passes only each case's request and evidence payload to Investigator/rules. Scenario IDs, catalog metadata, fixture controls, labels, and oracle fields remain with the operator/scorer. Out-of-scope and insufficient-evidence controls are denied before model work; the projection is not permission to call the provider.

Validate a private trial receipt before handing it to S6 with:

```sh
uv run --project services/backend --frozen python scripts/release_scenarios.py validate-receipt <receipt.json>
```

## Owned labs

`scripts/manage-local-lab.py` manages two owned projects. Select one with both `CLOUDOPS_LAB_PROJECT` and `--project`:

| Project | Directory | Checkout sample | Used for |
| --- | --- | --- | --- |
| `cloudops-local-testing-lab` | `tmp/local-testing-lab` | Triage sample (`sample_app.py`) | Existing E01–E08 lab work, Pending and ImagePull observations |
| `cloudops-e10-s3-lab` | `tmp/e10-s3-lab` | E07 recovery sample (`Dockerfile.recovery`) under `Recreate` | All recovery scenarios, plus Pending and ImagePull |

Each project has its own volumes, owner marker and project-named images, so the recovery lab never adopts or overwrites the retained general lab. Recovery trials, recovery faults, `recovery-config`, `qualify` and `writer` are refused outside `cloudops-e10-s3-lab`, and recovery receipts must name that project. The recovery lab adds `compose.lab-recovery.yaml`, which mounts the operator-generated `RecoveryPolicy/v1` and `RecoveryVerificationProfile/v1` files read-only into the API, worker and executor.

The manager reads the K3s container's `/proc/<pid>/cgroup` to prove containment, so it must run in the Docker engine's PID and cgroup namespace. On a Linux Docker host, run it directly. On Docker Desktop, run it in a small helper container with `--pid=host --cgroupns=host`, the Docker socket, read-only `/sys/fs/cgroup` and the checkout mounted at the same absolute path. Registry digest checks during `start` use an empty Docker configuration, so they are subject to anonymous registry rate limits.

## Fault mapping

| Scenario | Local fault | Field the approved action repairs | Treatment kept fixed |
| --- | --- | --- | --- |
| `recovery-oom-memory` | `memory-limit`: 128Mi request and limit for the 160 MiB resident sample | Memory request/limit (`set_memory_profile`) | Offered load |
| `recovery-bad-rollout-restore` | `bad-rollout`: the sample's `bad-image` variant, which rejects startup | Image (`restore_deployment_template` to the confirmed known-good image) | Offered load |
| `recovery-traffic-scale` | `overload`: no field change; 30 rps against one Pod with two serving slots | Replicas (`scale_deployment` to 2) | Offered load |
| `pending-insufficient-cpu` | `pending`: 100-CPU request | Reset only | — |
| `imagepull-missing-manifest` | `imagepull`: unavailable local registry reference | Reset only | — |

Untolerated-taint and registry-authorization variants remain fixture evidence; the manager refuses to substitute a different live fault.

## Recovery qualification

Qualification precedes any measured trial and is never inferred from a first positive. From the healthy recovery-lab baseline:

```sh
export CLOUDOPS_LAB_PROJECT=cloudops-e10-s3-lab
lab="uv run --project services/backend --frozen python scripts/manage-local-lab.py --project cloudops-e10-s3-lab"
$lab start
$lab recovery-config
$lab qualify recovery-traffic-scale --rate 30 --seconds 210
$lab qualify recovery-oom-memory --rate 8 --seconds 330
$lab qualify recovery-bad-rollout-restore --rate 8 --seconds 330
```

`recovery-config` derives the exact selected containers, source digest and canonical digests inside the API process, writes the policy and one profile per scenario, and refuses to finish unless the API loads exactly those profiles. `qualify` is a trusted operator calibration, not a product action. It applies the profile's post-action state and offers the fixed load for at least the profile window plus 30 seconds. Over the full window (5 samples for 180 seconds, 9 for 300) it samples the profile's own queries every 30 seconds after the 60-second warmup: request rate, error ratio, p95 latency, offered rate, per-Pod counter resets and sample freshness. It then restores the baseline and writes `qualification/<scenario>.json` only if every sample meets the profile. The receipt binds the measured rate, and a trial's recovery load must use exactly that rate. Failed calibrations are retained beside it.

## Trial and reset flow

Every `trial begin` allocates fresh trial/run/window UUIDs and every reset a fresh reset UUID. Begin and reset each offer a bounded 40-second, 5 rps readiness load so the target's health is measured, then bind the target UID, source/metrics fingerprint, runtime image IDs, declared resources and measured surge headroom. They refuse an unexpired eligible action, an active execution, an unquarantined writer, a foreign UID, a metric sample older than 120 seconds or a changed source binding. Insufficient headroom makes a trial `manual` and a reset receipt `incomplete`. While a recovery trial is active, no other targeted load may run and a second recovery load is refused. A reset outside an E10 trial keeps the general lab's restore-only contract.

Healthy, faulted and interrupted resets:

```sh
$lab trial begin pending-insufficient-cpu && $lab reset
$lab trial begin pending-insufficient-cpu && $lab fault pending && $lab observe && $lab reset
$lab trial begin pending-insufficient-cpu && $lab fault pending && $lab reset --interrupted
```

A recovery trial without a qualification receipt starts as `manual`, and its fault is refused. A bad-rollout trial is also `manual` unless the latest confirmed E02 baseline equals the healthy sample. Confirm it from the idle healthy target with `python scripts/probe-e10-s3-recovery.py baseline` before the trial; the trial and its receipts record that baseline's ID and digest. A qualified recovery trial runs:

```sh
$lab trial begin recovery-traffic-scale --qualification-receipt tmp/e10-s3-lab/qualification/recovery-traffic-scale.json
$lab load --target checkout --seconds 900 --rate 30 --recovery --detach
sleep 60
$lab fault overload
$lab observe
$lab writer enable
python scripts/probe-e10-s3-recovery.py act --scenario recovery-traffic-scale
$lab trial post-check --execution-receipt <execution.json> --verification-receipt <verification.json>
$lab writer quarantine
$lab load-stop --interrupt
$lab reset          # after the approved plan's five-minute expiry
```

`writer enable` opens the isolated gateway only for an observed recovery trial: it uses the root maintenance socket to enable the exact target and exit recovery mode. `writer quarantine` closes dispatch and the target again before load finalization and reset. The probe signs in as an ordinary Operator over same-origin HTTPS, opens a manual incident with a neutral request that names no scenario, fault or control, waits for its Run, proposes the qualified plan, checks that it is bound to the qualified verification profile, approves it explicitly and saves the execution and verification read responses privately. The UI can replace the probe; the post-check accepts only those E07 read responses.

`post-check` requires the same running offered load, the observed fault record, the exact qualified action state and a passed E07 verification over its complete fixed window. A reset after an interrupted or unverified recovery trial restores and verifies the baseline, writes an `incomplete` receipt with the reason, and clears the trial so the next trial starts fresh. Receipts are written owner-only under `tmp/<lab>/receipts/`.

## Live acceptance

[`live-acceptance.json`](live-acceptance.json) is the sanitized record of one run on owned single-node K3s in a freshly started `cloudops-e10-s3-lab`. The lab was started and every receipt written at source commit `a76e718` with an empty working diff. All nine trial reset receipts pass `validate-receipt`.

Qualification, before any trial (operator calibration of each profile's post-action state over its full window after warmup):

| Scenario | State | Offered load | Samples | Worst sample |
| --- | --- | --- | --- | --- |
| `recovery-traffic-scale` | 2 replicas | 30 rps | 5 (180 s) | 30.3 rps served, error ratio 0.0012, p95 0.243 s |
| `recovery-oom-memory` | 192Mi/256Mi | 8 rps | 9 (300 s) | 8.0 rps served, error ratio 0, p95 0.243 s |
| `recovery-bad-rollout-restore` | Known-good image | 8 rps | 9 (300 s) | 8.0 rps served, error ratio 0, p95 0.243 s |

Every sample had the expected serving Pods, no counter reset and a sample under 5 seconds old. Each receipt binds its rate.

Trial resets, each with fresh trial/run/window/reset IDs and zero residual eligible actions and zero duplicate targeted loads:

| Scenario | Reset from | Observed fault | Receipt |
| --- | --- | --- | --- |
| `pending-insufficient-cpu` | Healthy baseline | — | `reset_complete` |
| `pending-insufficient-cpu` | Fault | `Unschedulable`, `FailedScheduling` | `reset_complete` |
| `imagepull-missing-manifest` | Fault | `ErrImagePull`, `BackOff` | `reset_complete` |
| `pending-insufficient-cpu` | Fault without observation (`--interrupted`) | — | `reset_complete`, `incomplete_observation` |
| `recovery-oom-memory` without qualification | Healthy baseline | Fault refused | `manual` |
| `recovery-oom-memory` | Load stopped early after observation | `OOMKilled` (exit 137) under 8 rps | `incomplete`: no positive E07 post-check; baseline restored |
| `recovery-bad-rollout-restore` | Load stopped early after observation | Startup rejection, `BackOff` under 8 rps | `incomplete`: no positive E07 post-check; baseline restored |
| `recovery-traffic-scale`, refused reset | Reset attempted while the trial's load ran | Error ratio above 0.1 | `incomplete`: refused, trial state retained |
| `recovery-traffic-scale`, same trial | Fault, after positive post-check | Error ratio above 0.1 | `reset_complete` with the E07 post-check |

Before the bad-rollout trial, an Operator confirmed the healthy sample as an E02 known-good baseline through the API. The trial and its receipt record that baseline. The OOM trial refused a load at a rate other than its qualified 8 rps. The traffic trial refused a second targeted load and a duplicate recovery load, and its inject time is the start of its fixed load.

The traffic trial then ran through the normal product path. An Operator opened a manual incident with a neutral request, proposed the qualified `RecoveryPlan/v2` scale to two replicas, read its review and approved it. E07 acknowledged the change and finalized `passed` over its full 180-second window, with 5/5 rollout samples, full coverage and 31.2 rps offered against the fixed 30 rps. The same offered load ran from before detection until after the post-check.

Limits:

- `set_memory_profile` and `restore_deployment_template` plans are created only from an E06 analysis. Their positive post-checks need an approved model budget and belong to S6. This run covers their qualification, the known-good baseline, the real faults under load, their observation and verified baseline resets.
- Untolerated-taint and registry-authorization variants remain fixture evidence.
- One owned single-node lab. No shared, cloud or production target, and no scoring, SLO or recovery-success rate.

This slice prepares local resets and S6 inputs. It does not claim the fifteen live recovery trials or final scoring, which remain in S6.
