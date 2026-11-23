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

## Owned-lab reset flow

Use the retained `cloudops-local-testing-lab` project and its generated private directory from `docs/e01-local-lab.md`. The manager binds each trial to the catalog version, run/window IDs, owned `demo/checkout` Deployment UID, reader/metrics source fingerprint, runtime image IDs, bounded resource requests, and measured single-node surge headroom. It refuses an unexpired action, active execution, foreign UID, stale metrics, changed source binding, or insufficient headroom.

For a reset from a known healthy state:

```sh
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab trial begin pending-insufficient-cpu
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab reset
```

For an interrupted controlled fault, the manager persists the trial and fault records before returning. After the fault operation, reset the retained state with:

```sh
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab trial begin pending-insufficient-cpu
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab fault pending
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab reset --interrupted
```

The fault command's fixed controls can generate the CPU Pending and missing-manifest ImagePull local cases. Untolerated-taint and registry-authorization variants remain fixture evidence; the manager refuses to substitute a different live fault. `observe` captures bounded event/log evidence before reset when a trial needs an observed fault record.

Every begin allocates fresh trial/run/window UUIDs. Each reset attempt allocates a fresh reset UUID. Successful reset receipts require the original target UID, exact restored fields, healthy ready Pods, fresh UID-bound source metrics, measured surge headroom, zero unexpired eligible actions, and zero duplicate targeted loads. Receipts are written owner-only under `tmp/local-testing-lab/receipts/`; incomplete receipts and their fault/trial state are retained for inspection.

Recovery scenarios require an `E10RecoveryQualificationReceipt/v1` file passed as `--qualification-receipt`. It must bind the scenario, exact target UID, source-binding digest, runtime image IDs, qualified profile digest, exact post-action replica/template/rollout state, and required surge CPU/memory. The template must include every reset-controlled field, including the injected `LAB_MODE` when the action leaves that treatment in place. Missing qualification creates a manual readiness receipt and the fault command refuses injection. The operator does not use this local fault tool to execute product actions; use the normal E07 recovery path and its explicit approval/post-check receipts.

For a qualified recovery trial, start the trial, then its bounded recovery load, then inject the mapped local fault. The local mappings are OOM → `oom`, bad startup rollout → `crashloop`, and traffic overload → `traffic`:

```sh
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab trial begin recovery-oom-memory \
  --qualification-receipt <private-qualification.json>
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab load --target checkout --seconds 900 --rate 30 --recovery --detach
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab fault oom
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab observe
```

Use the normal E07 product action with its explicit approval and fixed positive post-check. Keep the offered load and injected fault treatment in place for that whole post-check, and record the E07 evidence before finalizing the load as described below. The manager restores only the recorded baseline, fixed fault state, or exact qualified post-action state using a UID/resourceVersion conditional patch. An interrupted run can use `load-stop --interrupt`, followed by `reset --interrupted`; its receipt retains the interrupted load state. Reset refuses to complete while any targeted load remains active or while the live target differs from those bound states.

The offered-load exporter is scraped as an optional Prometheus target while its exact owned container is active. Fault injection and observation require fresh application metrics plus a fresh offered-rate sample bound to the trial Deployment UID; the observed offered rate must remain within 20% of the fixed requested rate. Wait for the initial scrape/rate window before injecting the fault. The E07 product verification remains the source of the positive post-check result; the reset receipt records the independent offered-load samples and cleanup state.

After the E07 recovery action succeeds, keep the same recovery load running through its complete fixed positive verification window. Save the JSON responses from `GET /api/executions/{execution_id}` and `GET /api/executions/{execution_id}/verification` as private files inside the lab's owner-only directory, then bind them to the active trial:

```sh
uv run --project services/backend --frozen python scripts/manage-local-lab.py \
  --project cloudops-local-testing-lab trial post-check \
  --execution-receipt <private-execution.json> \
  --verification-receipt <private-verification.json>
```

Only after this command records `passed` may the load be finalized and the baseline reset. If its fixed duration has not elapsed, stop the still-running owned load with `load-stop --interrupt`; if it has already ended, use `load-stop`. Then run `reset`. The reset receipt carries the E07 execution/profile binding, full sample count, matching load-rate evidence, exact fault treatment, and final load state. Missing, failed, stale, or mismatched E07 evidence leaves the reset receipt incomplete; it cannot be promoted to `reset_complete`.

This slice prepares local resets and S6 inputs. It does not claim the fifteen live recovery trials or final scoring, which remain in S6.
