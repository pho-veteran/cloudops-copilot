# Release backup and restore

This is the supported operator procedure for backing up and restoring a release installation. A restore returns to a known database cutoff. Any post-cutoff loss is stated explicitly, and every write path stays closed until trusted reconciliation. It does not provide continuous recovery. Three authorities are external to PostgreSQL and are never restored from a database backup:

- the host dispatch permit (`job_dispatch` volume)
- the executor journal (`executor_authority` volume)
- the executor pin (`executor_identity` volume)

Run every trusted command on the deployment host as the deployment root. Application roles have no maintenance, permit or enablement endpoint. The [qualification receipts](evidence/e10-s5-restore/README.md) record one run of this procedure with release images.

## Compose function

Use the [release package](release-package.md) install directory and its `compose` function. Add `-f compose.integration.yaml -f compose.release-lab.yaml` for the scoped writer topology. Restore drills must not touch application data. Run them in a separate disposable project with `-f compose.release-restore.yaml`, which points every service at `cloudops_restore_test` and adds the trusted `restore-admin` service.

## Back up

A backup is a consistent `pg_dump` plus a manifest. Take it while the application runs. Store both privately and outside every install directory.

```sh
compose run --rm -T dispatch-admin status > backup/permit.json
compose exec -T executor python -m cloudops.features.recovery.maintenance status > backup/executor.json
compose exec -T api alembic -c services/backend/alembic.ini current > backup/migration.txt
compose exec -T db psql -U cloudops -d "$database" -At -c "select now(), pg_current_xact_id()" > backup/cutoff.txt
compose exec -T db pg_dump -U cloudops -d "$database" -Fc > backup/database.dump
sha256sum backup/database.dump > backup/database.sha256
```

The manifest must name all of the following:

- cutoff time and transaction
- dump SHA-256
- PostgreSQL version and migration head
- bundle revision and image IDs
- project
- configured writer target UIDs
- reader and writer scope digests
- executor deployment ID, pin digest and journal attempt list
- permit authority, generation and mode

`scripts/probe-release-restore.py cut` writes this as `ReleaseRestoreManifest/v1`, together with per-table row digests and evidence SHA-256 values. Never back up or replace the executor journal or pin as part of an application restore.

## Restore

1. Save `dispatch-admin status`. Then close host dispatch: `dispatch-admin enter-recovery --reason ... --evidence ...`.
2. Quarantine every configured writer target: `maintenance enter_recovery` with one `--target` per UID in `writer/workloads.json`, plus a reason and evidence.
3. Stop `worker`, `api`, `https` and `observer`. Keep `db` and `executor` running, so the journal, socket and quarantine remain available.
4. Verify the dump against its manifest. Then run `pg_restore --clean --if-exists --no-owner` into the intended database.
5. Run migrations: `compose run --rm --no-deps migrate`.
6. Retire restored authority:
   - `compose run --rm --no-deps restore-admin` runs `cloudops.entrypoints.recovery_restore`. It revokes restored sessions and review receipts, retires restored recovery jobs, marks active executions `outcome_unknown` while keeping their target lock, and invalidates proposed or retired plans. It refuses unless the permit is in recovery with every class denied and every gateway target is closed.
   - Then run `compose run --rm --no-deps restore-admin cloudops.entrypoints.diagnosis_recovery`.
7. Start `api` and `https`. The worker may start, but it claims nothing while the permit is in recovery.

While quarantined, workloads, incidents, runs, evidence and history stay readable. To investigate, resume only `collection` with `dispatch-admin resume --class collection`. Proposals and approvals for every configured target return `409 target_quarantined`. This includes a UID with no restored execution rows. A restored lock returns `409 target_locked`. Restarting the executor, repeating a maintenance request with the same `--request-id` or rerunning `restore-admin` adds no Kubernetes write. The executor refuses a late dispatch:

- With a retired epoch: `target_quarantined`.
- Reusing an execution identity with different content: `dispatch_conflict`.

## Reconcile and enable

1. Record the current state of each target: Deployment UID, generation, replicas and readiness, compared with the journal attempts and the restored execution history. Explain every difference before continuing.
2. Run `maintenance enable_target --target <UID>` for each target. Use the reconciliation record as evidence. Each call fences the Deployment with a UID/resourceVersion-tested annotation and checks health. Missing proof leaves the target disabled.
3. Run `maintenance exit_recovery_mode` with every configured target.
4. Restore host dispatch to the saved state. If it was `normal`, use `dispatch-admin exit-recovery`. Otherwise run `dispatch-admin resume --class` only for each class it allowed.
5. Users sign in again and read current plans. A restored approval never executes. A new write needs a fresh proposal, a review and an explicit approval.

## Unresolved scope

If the executor journal or pin is missing or replaced, the executor refuses to start (`authority_unavailable`). The application then returns `503 executor_unavailable`, and no maintenance channel exists. A target also stays quarantined when its fence proof fails or its health cannot be established. Do not initialize a new authority over retained history, and do not enable a target without proof. Handle the affected incidents manually outside the product. Recovery requires the original journal and pin, or a deployment-owner reconciliation of the writer scope. Records stay readable throughout.

## Qualification driver

From a verified install directory on Linux, set:

- `CLOUDOPS_LAB_PROJECT`, a new project name
- `CLOUDOPS_RELEASE_BUNDLE`
- `CLOUDOPS_HOST_ENV`
- `CLOUDOPS_RELEASE_RESTORE=1`

Then run `scripts/bootstrap-integration.py`, followed by these phases, with the Operator and Viewer passwords on stdin:

```sh
probe=(uv run --project services/backend --frozen python scripts/probe-release-restore.py)
"${probe[@]}" prepare                      # standby target, accounts, CA, baseline receipt
"${probe[@]}" reconcile --label initial    # first explicit enablement
"${probe[@]}" cut d1                       # proposed plan
"${probe[@]}" cut d2                       # approved, not dispatched
"${probe[@]}" cut d3                       # dispatched and acknowledged
"${probe[@]}" activity                     # post-cutoff records and live snapshot
"${probe[@]}" restore d1                   # then d2, d3
"${probe[@]}" reconcile --label after-d3 --dispatch-exit
"${probe[@]}" fresh                        # one new approved execution
"${probe[@]}" manual-exit                  # authority loss keeps quarantine
```

The `prepare` phase applies `config/lab/restore-standby.yaml`. It adds a second healthy writer target that has no application execution history. Receipts are written to the ignored `tmp/release-restore/`. Dumps, cookies and other private files stay in `tmp/release-restore/private/`. The `manual-exit` phase deletes the project's executor pin, so run it last and only in a disposable project.
