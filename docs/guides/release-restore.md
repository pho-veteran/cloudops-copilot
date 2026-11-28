# Release backup and restore

This is the supported operator procedure for backing up and restoring a release installation. A restore returns the database to a known cutoff. Anything after that cutoff is lost, and the procedure states the loss explicitly. Every write path stays closed until trusted reconciliation. It is not continuous recovery.

Three authorities live outside PostgreSQL. A database backup never contains them, so a database restore never restores them:

- the host dispatch permit (`job_dispatch` volume);
- the executor journal (`executor_authority` volume);
- the executor pin (`executor_identity` volume).

Run every trusted command on the deployment host as the deployment root. No application role has a maintenance, permit or enablement endpoint. The [qualification receipts](../evidence/e10-s5-restore/README.md) record one run of this procedure with release images.

## Scope and Compose files

The procedure applies to an installation with the scoped writer topology. Start from the [release package](release-package.md) `compose` function and add these files:

- `-f compose.integration.yaml -f compose.release-lab.yaml` for the executor, its journal and the writer and reader scope;
- `-f compose.restore-admin.yaml` for the trusted `restore-admin` cleanup service. It uses the installation database `cloudops`.

Restore drills must not touch application data. Run them in a separate disposable project, and add `-f compose.release-restore.yaml` last. That overlay points every service, including `restore-admin`, at `cloudops_restore_test`. In the examples below, set `database=cloudops` for an installation or `database=cloudops_restore_test` for a drill. Writer target UIDs are listed in the lab directory's `writer/workloads.json`.

**Core-only installation (no executor).** A core-only install has no writer, no journal and no recovery executions, because a plan cannot be proposed without an executor. Skip steps 2 and 6 and the reconciliation section. After the database restore, run `compose run --rm --no-deps api python -m cloudops.entrypoints.cli revoke-all-sessions`, then `compose run --rm --no-deps api python -m cloudops.entrypoints.diagnosis_recovery`; the API service already mounts the permit read-only. Restore host dispatch to the saved state afterwards. `scripts/probe-release-core-restore.py` qualifies this path in a disposable core project with `compose.release-core-restore.yaml`.

## Back up

A backup is a consistent `pg_dump` plus a manifest. Take it while the application is running, and store both privately outside every install directory.

```sh
database=cloudops
lab_dir=/path/to/install/tmp/e01-integration   # reader/writer scope of this installation
mkdir -m 700 backup
compose run --rm -T dispatch-admin status > backup/permit.json
compose exec -T executor python -m cloudops.features.recovery.maintenance status > backup/executor.json
compose exec -T executor sha256sum /run/cloudops-executor-identity/identity.json > backup/pin.sha256
compose exec -T executor python -c "import json,sqlite3; db=sqlite3.connect('file:/var/lib/cloudops-executor/authority.sqlite?mode=ro', uri=True); print(json.dumps(db.execute('select id, state from attempts order by rowid').fetchall()))" > backup/journal-attempts.json
compose exec -T api alembic -c services/backend/alembic.ini current > backup/migration.txt
compose exec -T db psql -U cloudops -d "$database" -At -c "select now(), pg_current_xact_id()" > backup/cutoff-before.txt
compose exec -T db pg_dump -U cloudops -d "$database" -Fc > backup/database.dump
compose exec -T db psql -U cloudops -d "$database" -At -c "select now(), pg_current_xact_id()" > backup/cutoff-after.txt
sha256sum backup/database.dump "$lab_dir/writer/workloads.json" "$lab_dir/reader/workloads.json" > backup/sha256.txt
cp "$bundle/manifest.json" backup/bundle-manifest.json
```

`pg_dump` reads one transaction snapshot. The cutoff lies between the two recorded transactions. The manifest names:

- the cutoff bounds;
- the dump SHA-256;
- the PostgreSQL version and migration head;
- the bundle revision and image IDs;
- the project;
- the writer target UIDs;
- the reader and writer scope digests;
- the executor deployment ID, pin digest and journal attempt list;
- the permit authority, generation and mode.

For drills, `scripts/probe-release-restore.py cut` writes these fields as `ReleaseRestoreManifest/v1`. The manifest also contains per-table and per-row digests and evidence SHA-256 values. The driver refuses a cutoff whose non-volatile table digests changed while the dump ran. Never back up, copy or replace the executor journal or pin as part of an application restore.

## Restore

Use a reason and a ticket or receipt reference as evidence for every trusted change.

1. Save the dispatch state, then close dispatch:

   ```sh
   compose run --rm -T dispatch-admin status > restore/permit-before.json
   compose run --rm dispatch-admin enter-recovery --reason "Restore <cutoff>" --evidence "<ticket>"
   ```

2. Quarantine every configured writer target. Pass one `--target` per UID, and include every UID:

   ```sh
   compose exec -T executor python -m cloudops.features.recovery.maintenance enter_recovery \
     --target <UID-1> --target <UID-2> --reason "Restore <cutoff>" --evidence "<ticket>"
   ```

   Continue only if the result reports `"status": "applied"`.

3. Stop `worker`, `api`, `https` and `observer`. Keep `db` and `executor` running so that the journal, socket and quarantine stay available.

4. Verify the dump against its manifest. Then run `compose exec -T db pg_restore -U cloudops -d "$database" --clean --if-exists --no-owner < backup/database.dump`.

5. Run the migrations with `compose run --rm --no-deps migrate`.

6. Retire restored authority:

   ```sh
   compose run --rm --no-deps restore-admin
   compose run --rm --no-deps restore-admin cloudops.entrypoints.diagnosis_recovery
   ```

   The first command runs `cloudops.entrypoints.recovery_restore`. It refuses unless the permit is in recovery with every class denied and every gateway target is closed. When it runs, it:

   - deletes restored sessions and plan review receipts;
   - fails queued, held and running recovery, reconciliation and verification jobs;
   - marks each active execution `outcome_unknown` and keeps its target lock. An active execution whose result was already acknowledged becomes `manual_intervention` without the lock;
   - invalidates proposed plans, and approved plans whose executions it retired.

   Run it only while the application processes are stopped.

7. Start `api` and `https`. The worker may also start; it claims nothing while the permit is in recovery.

### While quarantined

Workloads, incidents, runs, evidence and history stay readable. To investigate, resume only collection:

```sh
compose run --rm dispatch-admin resume --class collection --reason "Investigate after restore" --evidence "<ticket>"
```

Proposals and approvals for every configured target return `409 target_quarantined`. This includes a UID with no restored execution rows. A restored lock returns `409 target_locked`.

None of the following writes to Kubernetes or the journal:

- restarting the executor;
- repeating a maintenance request with the same `--request-id`, which returns its stored receipt;
- rerunning `restore-admin` while the application is stopped.

The executor refuses a late dispatch. A dispatch carrying a retired epoch returns `target_quarantined`, even after the target is enabled again. A dispatch that reuses an execution identity with different content returns `dispatch_conflict`.

## Reconcile and enable

1. **Record the current state of each target.** Capture the Deployment UID, generation, replicas and readiness. Compare them with the journal attempts and the restored execution history, and explain every difference before you continue.

2. **Enable each target.** Use the reconciliation record as evidence:

   ```sh
   compose exec -T executor python -m cloudops.features.recovery.maintenance enable_target \
     --target <UID> --reason "Reconciled after restore" --evidence "<reconciliation record>"
   ```

   Each call fences the Deployment with an annotation patch that tests the UID and resourceVersion, then checks health. A target that is not fully ready or has no proof stays disabled. The executor reports `maintenance_state_changed` or `maintenance_proof_required`. Repair the target manually first.

3. **Exit recovery mode.** Run `maintenance exit_recovery_mode` with every configured `--target`, a reason and evidence.

4. **Restore host dispatch to the saved state.**
   - If the saved state was `normal`, run `dispatch-admin exit-recovery`.
   - If it was `recovery`, run `dispatch-admin enter-recovery`. This also closes any class resumed only for investigation. Then run `dispatch-admin resume --class` once for each class in the saved state.
   - Every command needs a reason and evidence.

5. **Users sign in again and read current plans.** A restored approval never executes. A new write needs a fresh proposal, a review and an explicit approval.

## Unresolved scope

Missing or replaced journal or pin:

- The executor refuses to start with "Pinned executor authority is missing or changed".
- If the pin changes while the executor is running, its admissions return `503 authority_unavailable`.
- With the executor down, the application returns `503 executor_unavailable` and no maintenance channel exists.

Failed fence proof, or health that cannot be established, also keeps the target quarantined.

In every case:

- Do not initialize a new authority over retained history.
- Do not enable a target without proof.
- Handle the affected incidents manually outside the product. Records stay readable.
- Recovery requires the original journal and pin, or a deployment-owner reconciliation of the writer scope.

## Qualification driver

Run from a verified install directory on Linux, with these variables set:

- `CLOUDOPS_LAB_PROJECT` (a new project)
- `CLOUDOPS_RELEASE_BUNDLE`
- `CLOUDOPS_HOST_ENV`
- `CLOUDOPS_RELEASE_RESTORE=1`

The scripts then add `compose.restore-admin.yaml` and `compose.release-restore.yaml`. Run `scripts/bootstrap-integration.py`, then these phases with the Operator and Viewer passwords on stdin:

```sh
probe=(uv run --project services/backend --frozen python scripts/probe-release-restore.py)
"${probe[@]}" prepare                    # standby target, accounts, CA, baseline receipt
"${probe[@]}" reconcile --label initial  # first explicit enablement
"${probe[@]}" cut d1                     # proposed plan
"${probe[@]}" cut d2                     # approved, not dispatched
"${probe[@]}" cut d3                     # dispatched and acknowledged
"${probe[@]}" activity                   # post-cutoff records and live snapshot
"${probe[@]}" restore d1                 # then d3, then d2 (journal ahead of the database)
"${probe[@]}" reconcile --label after-d2 --dispatch-exit --prove-failure checkout-standby
"${probe[@]}" fresh                      # one new approved execution
"${probe[@]}" late-writer                 # a real admitted patch held past a real fence
"${probe[@]}" manual-exit                # authority loss keeps quarantine
```

What the phases do:

- **`prepare`** applies `config/lab/restore-standby.yaml`. This adds a second healthy writer target with no application execution history.
- **`--prove-failure`** scales that target to zero before enabling it. This is a deliberate out-of-protocol lab action. It records the denied enablement, then restores one replica.
- **`late-writer`** runs `scripts/probe-late-writer.py` inside the executor. It delays one admitted scale patch, fences the target with `release_uncertain_target`, then releases the delayed patch, which Kubernetes must reject. The unknown outcome stays in the journal and the application.
- **`manual-exit`** deletes the project's executor pin. Run it last, and only in a disposable project; every destructive phase refuses a project it did not prepare.

Receipts go to the ignored `tmp/release-restore/`. Dumps, cookies and other private files go to `tmp/release-restore/private/`.
