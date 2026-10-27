# Local access, consumer and executor integration

This is an operational handoff for the optional, disposable `cloudops-e01-https` lab. E01 acceptance and the continuing E02/E03/E07/E08/E09/E10 scope remain in the [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) and linked EPICs. The default investigation collector is still synthetic. An Operator can separately propose an absolute scale using fresh live Kubernetes observations; collector output is never treated as a diagnosis or execution prerequisite.

## Start the isolated lab

Use Docker Desktop/Engine with Linux containers, Compose 2.24.4 or newer, and the README `.env` setup. The K3s server is a privileged **lab container**, not access to a host kubeconfig or an external cluster. All published endpoints remain on loopback. Host Python/uv are needed for the setup/probe scripts; runtime services use their locked container dependencies.

```sh
uv run --project services/backend python scripts/bootstrap-integration.py
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml up -d --build --wait --wait-timeout 240 api worker observer executor krr https
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -m cloudops.entrypoints.cli account viewer --role viewer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml cp https:/data/caddy/pki/authorities/local/root.crt tmp/local-https-root.crt
```

The bootstrap applies `config/lab/workload.yaml` only to its own K3s service, waits for `demo/checkout`, captures its actual UID and creates separate eight-hour ServiceAccount tokens under ignored `tmp/e01-integration`. It never prints token values. Refresh credentials by rerunning setup with product writes quarantined, synchronizing workloads through the CLI and restarting the executor; restart always requires new enable/exit proof. Changing a Deployment UID preserves the prior application identity as inactive history.

The API mounts the read-only observation connection. The worker additionally mounts private dispatch/KRR request keys. Only the isolated executor mounts the Kubernetes writer connection and maintenance socket; neither application container mounts them or a Docker socket. Kubernetes RBAC permits that writer to patch only the named Deployment in `demo`; reader/writer accounts cannot read Secrets. Capacity checks conservatively support the lab's single schedulable node. Other placement/controller cases are denied for manual review.

The executor starts in independent recovery mode with every target disabled, including after a container restart. Read the UID from `tmp/e01-integration/writer/workloads.json`, then use the real deployment-root channel:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance status
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance enable_target --target <UID> --reason "Initial local readiness" --evidence "local://operator/current-state"
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance exit_recovery_mode --target <UID> --reason "Explicitly enable the reconciled lab" --evidence "local://operator/current-state"
```

The evidence strings are audit references, not the safety proof. The executor itself writes a unique nonfunctional annotation through an atomic UID/resourceVersion-tested patch and reads it back with current health. Application roles have no maintenance endpoint or override. The Unix server obtains UID/PID with `SO_PEERCRED`; no caller-supplied actor is accepted. The recorded actor is isolated deployment-root, UID/PID and deployment ID, not an invented individual operator identity. Non-root peers receive a durable denial. Every mutation needs a reason, evidence references and exact configured targets; global recovery operations require all configured targets.

Open `https://localhost:8443` after accepting this lab's CA through normal local certificate setup. The request probes take `--ca` and validate the real certificate chain and hostname; they never disable TLS validation. Browser trust is a separate workstation choice. Remove any temporary trust added solely for testing after the browser exercise; preserve pre-existing trust.

## Consumer behavior

- Workloads expose selected image/resource baseline candidates from two matching healthy observations at least five seconds apart, valid for five minutes. Confirmation saves UID/digest/actor/time metadata and never patches Kubernetes. Referenced configuration and Secrets are explicitly outside the snapshot.
- Incident notes and acknowledge/resolve/reopen use the existing principal. Resolution captures an immutable stored handoff; later notes/reopen cannot rewrite that export. Queued/executing recovery blocks resolution and reinvestigation. An unknown execution may remain visible in a resolved incident and retains its lock until trusted proof.
- Connection checks have fixed `cluster`, `database` and `worker` destinations. Results are persisted observations; no request accepts an endpoint or credential.
- KRR runs the pinned 1.31.1 tool against the configured read-only cluster and Prometheus. The lab has no representative container history. Its actual report is `insufficient_data`, with tool exit/output digest and null coverage/recommendations; no savings or apply result is fabricated. E08 still owns qualified idle/steady/peak history and recommendation quality.
- Recovery currently supports only Operator-proposed `scale_deployment`, absolute replicas 1–3. The immutable plan contains source/UID/current resourceVersion/selected before fields, rationale, digest, target epoch and five-minute expiry. Live UID/controller/capacity checks run at preview, decision and dispatch. One durable target lock and a thirty-second admission cooldown prevent overlapping/repeated changes. Other recovery templates remain unavailable.
- Reading a plan returns live validity and a receipt tied to the current session, expiring within sixty seconds. Approval/rejection requires its exact version/digest and a fresh explicit action. Reauthentication invalidates old receipts. The UI binds the confirmation dialog to the reviewed plan and discards intent when authority or the displayed plan changes. No mutation is replayed automatically.
- An acknowledged Kubernetes patch is **not** verified remediation. Results remain `manual_intervention`/`inconclusive`; rollout, application metrics and any subsequent change require manual assessment. There is no automatic rollback.

Viewers read/export these records and log out their own session. All ten Operator mutation operations reject a valid Viewer request before any committed domain/job change. Anonymous/expired requests cannot read observations, review receipts or stored records. The actual 34-operation inventory and executable role cases are in `services/backend/tests`; provider fixtures are explicitly separate from the live probes.

## Observed workload health

The dedicated `observer` service runs `cloudops.entrypoints.observer` every `OBSERVATION_POLL_SECONDS` (default 30 seconds). It is separate from the collection worker and the shared `jobs` queue: it never claims, renews or completes an application Job, so a slow or unavailable cluster read cannot consume admission or recovery execution capacity. Each configured live workload has at most one observation lease (45 seconds); a stale lease or a changed Deployment UID cannot publish. Each sweep claims every due workload in the configured scope (up to 100) and reads them with `OBSERVATION_CONCURRENCY` workers, then sleeps only the remainder of the poll interval; a successful publication releases the lease immediately, so the next due time still holds the 30-second cadence.

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml ps observer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml logs --tail 100 observer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml restart observer
```

The integration overlay mounts only `./tmp/e01-integration/reader` at `/run/cloudops-reader` in the observer. It has no writer kubeconfig, dispatch key, KRR credential or Docker socket. The API still mounts the reader connection only for the protected read routes; the worker keeps its separate private dispatch/KRR credentials.

Workloads shows the persisted projection, never a request-time Kubernetes read:

- **Fresh** means the last successful observation is at most 60 seconds old. **Stale** means it is older than 60 seconds; the prior values remain visible and are labeled historical, not healthy. **Unavailable** means no successful observation has been stored.
- Status is one of Healthy, Progressing, Degraded, Scaled to zero or Unknown. Partial coverage names the missing sources (for example `deployment_status`). Observed reasons are sanitized codes such as `rollout_incomplete` or `progress_deadline_exceeded`.
- The panel shows observed/attempt times, readiness, restart total and a restart delta that is only measured for the same Pod UID and container set; a replaced Pod set reports the delta as unavailable instead of zero. It also shows generation, whitelisted template fields (image, pull policy, environment **names**, CPU/memory, ports), configuration reference names and observed Pod readiness. It never shows environment values, Secret data, Pod messages or provider response bodies. Application telemetry remains explicitly unverified.
- **Request observation refresh** is Operator-only. The API records a pending request and returns `202`; it does not call Kubernetes. The observer performs the read on its next cycle and deduplicates stacked requests. A Viewer sees the saved projection without a refresh control.
- Related incidents use the exact saved workload UUID before pagination and link to that incident ID. A replacement Deployment UID creates a different workload record, so old incidents remain with the retired identity.

If the observer is stopped, existing values become **Stale** after 60 seconds; nothing is relabeled fresh by restarting the API or worker. Restart the observer, check its logs for sanitized cycle failures, and use an Operator refresh if the next poll is not soon enough. A cluster read denial records a sanitized failure code such as `cluster_unavailable`; it does not expose the provider message. If the observer cannot be started, the last success stays historical and no new health is claimed.

Migration `0004_workload_observations` adds the projection/lease/history tables. Observation history is bounded to 100 successful rows and 100 attempt rows per workload, and to seven days. These lab notes and the focused tests are fixture/unit evidence unless a real scoped K3s browser receipt is captured; see `docs/evidence/e02-workload-observations.json`.

## Writer and maintenance protocol

The private executor serializes admission with a Linux file lock and journals the exact attempt before sending a conditional patch. Exact retries retrieve a durable outcome instead of writing again; changed content under an old execution ID is rejected. All functional patches test both UID and resourceVersion and replace an absolute replica count. A lost acknowledgement keeps `outcome_unknown` and quarantine.

Maintenance closes admission and retires the target epoch first. Already queued old epochs are rejected. Its acknowledged annotation fence changes the resourceVersion; read-back and explicit inequality with captured unresolved versions exclude already admitted late patches through the Kubernetes atomic precondition. ResourceVersions are compared for equality only, never numerically ordered. A five-second health observation bound is only a readiness check, not old-writer proof; neither elapsed time nor an expired worker lease proves exclusion.

`release_uncertain_target` can remove the application safety lock only after proof, retaining historical `outcome_unknown` and leaving the target disabled. `enable_target` and global `exit_recovery_mode` are separate explicit proof-backed operations. A durable `maintenance_released` marker prevents late worker publication from relabeling or relocking the historical outcome. SQLite attribution/exclusion/audit history resides in the separate executor authority volume, outside application PostgreSQL. Status exposes retained unknown attribution even across application restore. An unavailable proof provider denies enable/release/exit.

Use `--request-id <UUID>` to retrieve an exact maintenance request's durable receipt without repeating it. A replay includes current target states; historical success does not re-enable a target after restart. Preserve the local authority volume and single-host shared-lock topology. Replacing its storage or moving to another topology requires deployment-owner reconciliation, not copying a PostgreSQL lock bit or deleting the journal.

## Restore and restart

1. Enter recovery through the deployment-root CLI, covering every target; stop API and worker before restoring application data. Preserve the executor authority volume and Kubernetes identity.
2. Restore the known application backup, then run the current image's `cloudops.entrypoints.cli revoke-all-sessions` **before starting the API**. This removes sessions resurrected by an older database snapshot without hand-editing rows. Apply required migrations and synchronize scope through the CLI.
3. Restart the executor/API in independent quarantine. Inspect actual target state, retained unknown attribution and root audit receipts. Do not infer readiness from restored application rows.
4. Establish current UID/fence/health proof, explicitly enable each target, explicitly exit recovery mode, and only then resume workers/product writes. Users sign in and read current records/plans again. Previously admitted old-epoch work cannot authorize a new write.

Migration `0003_e01_consumers` adds eight feature tables plus a source column; populated upgrade preserves existing foundation records and assigns old workloads `development_fixture`. `0004_workload_observations` then adds the observation projection, lease, success history and attempt history tables; it chains from `0003_e01_consumers` as the single head. Repeat upgrade and metadata drift checks are tested. Downgrade removes new feature data and does not undo lifecycle values or Kubernetes effects; use a quarantined backup recovery rather than an application rollback assumption.

## Reproduce the local receipts

The final browser exercise served source `cfc131b0bbfde65a9289b1cb589cb8105064775c` through the actual Caddy HTTPS origin. After explicit user approval, the lab CA was temporarily installed in the current-user root store; the browser reported a secure TLS 1.3 connection. No certificate warning or validation setting was bypassed. The exact temporary CA was removed after testing, and normal eight-hour API sessions were restored.

The browser observed Secure/HttpOnly/Lax cookie attributes. A controlled cross-site iframe received 401 with Chromium `SchemefulSameSiteLax` cookie blocking; an authenticated top-level safe GET received 200. A real 45-second session expired with an approval dialog open, cleared the intent, focused sign-in and preserved the incident/run URL. Signing in fetched the plan again and sent no decision automatically. A held stale review receipt was denied; a new explicit confirmation admitted exactly one execution, acknowledged by Kubernetes with application verification still inconclusive. Viewers inspected the same saved records, exported handoff/KRR results and had no mutation controls. Second-tab sign-out caused the first tab's protected request to return 401.

The [sanitized browser receipt](evidence/e01-browser-https.json) distinguishes captured network windows from UI/persistence observations, including bounded debugger-buffer eviction. Actual 320/390 CSS layouts had no horizontal document overflow. Screenshots: [expiry at 320](evidence/e01-https-expired-320.jpg), [invalid login](evidence/e01-https-invalid-login-320.jpg), [fresh approval](evidence/e01-https-approval-320.jpg), [Viewer dark at 320](evidence/e01-https-viewer-320-dark.png) and [Viewer dark at 390](evidence/e01-https-viewer-390-dark.png). A subsequent copy correction distinguishes an ordinary active execution lock from an unknown outcome that needs trusted maintenance; access/protocol behavior is unchanged.

```sh
uv run --project services/backend python scripts/smoke-https.py --ca tmp/local-https-root.crt
uv run --project services/backend python scripts/smoke-e01-consumers.py --ca tmp/local-https-root.crt
uv run --project services/backend python scripts/probe-maintenance-denials.py
uv run --project services/backend python scripts/probe-lab-credentials.py
uv run --project services/backend python scripts/probe-late-writer-host.py
uv run --project services/backend python scripts/probe-local-restore.py
uv run --project services/backend python scripts/probe-expired-approval.py
```

The HTTPS/consumer/restore probes prompt for existing Operator and Viewer lab passwords; expiry and late-writer hosting need Operator only. Automation may opt into `--password-stdin`. Passwords are not command arguments, files or output. All probes create labeled local records. The expiry probe temporarily uses four-second sessions, restores normal API configuration in `finally`, observes `401 → 409 → 200 → 409`, and proves exactly one execution after a fresh read/explicit approval.

For the controlled late-writer probe, stop the lab worker, copy `scripts/probe-late-writer.py` into the executor at `/tmp/probe-late-writer.py` and run it there. At its `READY` message, use the real root CLI to enable/exit, propose/read/approve a fresh scale plan through the actual HTTPS API, and copy a JSON object containing that queued `execution_id` to `/tmp/e01-late-request.json`. The probe delays only the functional transport. The real maintenance server fences the real K3s Deployment; releasing the old request must produce a Kubernetes precondition denial. It checks preserved PostgreSQL unknown attribution, rejection of old epochs and late worker results, then saves sanitized `/tmp/e01-late-proof.json`. Copy that file to `tmp/e01-late-proof.json` on the host.

With that receipt and the consumer smoke identities present, `scripts/probe-local-restore.py` performs a real `pg_dump`/`pg_restore` in this disposable project, revokes restored sessions through the CLI, verifies independent quarantine/unknown history, then requires fresh proof and a new explicit proposal/decision. It resumes the worker and removes its temporary database backup only after passing. The application database used by these probes is distinct from the `_test` database cleared by pytest.

Sanitized committed runtime receipts and browser evidence are linked from PR #25 and E01. Producer/consumer review is by one active maintainer with real request/contract/state tests; no second reviewer is claimed. These local receipts do not close the other EPICs' full feature or release acceptance.
