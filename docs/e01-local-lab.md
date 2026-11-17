# Local access, consumer and executor integration

This is an operational handoff for the optional, disposable `cloudops-e01-https` lab. E01 acceptance and the continuing E02/E03/E07/E08/E09/E10 scope remain in the [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) and linked EPICs. The default investigation collector is still synthetic. An Operator can separately propose an absolute scale using fresh live Kubernetes observations; collector output is never treated as a diagnosis or execution prerequisite.

## Retained local testing lab

`manage-local-lab.py` extends the existing integration stack as one owned project,
`cloudops-local-testing-lab`. It retains K3s 1.37.1 at the existing digest, uses
Prometheus 3.13.5 and PostgreSQL 17.12, and adds a measured sample application and
minimal kube-state-metrics. It does not install host tools or touch host kubeconfig,
routing, certificate trust or unrelated Docker resources. Host Python 3.13 with the
backend's locked development dependencies supplies the driver and YAML parser.

From this checkout, use an explicit project and generated directory:

```sh
LAB_DIR="$PWD/tmp/local-testing-lab"
LAB=(uv run --project services/backend --frozen python scripts/manage-local-lab.py
  --project cloudops-local-testing-lab --directory "$LAB_DIR")
"${LAB[@]}" preflight
"${LAB[@]}" start
"${LAB[@]}" status
```

Preflight checks the local Linux/amd64 Docker daemon, cgroup v2, bounded-swap support,
ports, disk headroom and CIDR overlap. Start verifies registry references before
building, imports the source-hashed sample image into the owned node, and records
actual image identities. A missing marker with existing project resources, a foreign
ownership label, changed Deployment/namespace UID, or missing/replaced recorded
data/authority volume is a refusal, never adoption.
K3s data, kubelet state, CNI state and logs each use an explicitly named owned volume;
unexpected or anonymous K3s mounts are refused. There is no automatic volume adoption
or migration of older generated state.
Registry verification uses Buildx and checks pinned digests. Public image access uses
an empty private Docker configuration; host authentication and builder routing are
not inherited or modified.
Each successful registry result is saved immediately. An unchanged digest-pinned
reference may reuse that result only with a matching local Linux/amd64 artifact;
mutable build bases still require registry verification. Rate limits stop incomplete
setup rather than triggering a credential, tag or mirror fallback.
Repeated start preserves accounts, existing credential scope, PostgreSQL and the
independent executor/dispatch journals. It does not repair missing identity by
replacing a workload. The retained database is **cloudops_lab**; pytest is configured
for **cloudops_checks_test**, a separate database which must be created explicitly.
Restore drills must use **cloudops_restore_test** in a separate owned disposable
project. The older fixed-project restore probes are not retained-lab restore commands.

The private directory is mode 0700. Its generated `lab.env`, credential files and
receipts are mode 0600. `reader/`, `writer/`, `monitoring/`, `dispatcher/`, `krr/` and
intake subdirectories are mounted separately; fault/reset records in the parent
folder are never mounted into the product. ServiceAccount `expiry.json` files record
the actual issued expiry. No password, private key or bearer token is printed.
Generated lab state is excluded from Git and Docker build contexts.
The application consumes `reader/scope.json`, a v2 namespace/name policy pinned to
the reader's server and CA. KRR retains its separate exact-UID `reader/workloads.json`
list; the executor uses only `writer/workloads.json`. Both exports are checked
against recorded workload identities. Renewal cannot silently change the cluster pin.

Start does not provision accounts. To create the two lab accounts once, use the
existing CLI; the command prompts for a password. Calling `account` for an existing
username intentionally resets its password and revokes sessions, so do not repeat it
as part of startup:

```sh
DC=(docker --host unix:///var/run/docker.sock compose --env-file "$LAB_DIR/lab.env"
  -p cloudops-local-testing-lab --profile observation
  -f compose.yaml -f compose.https.yaml -f compose.integration.yaml
  -f compose.alertmanager.yaml -f compose.lab.yaml)
export DOCKER_CONFIG="$LAB_DIR/docker-config"
# Ensure ambient interpolation cannot replace the generated lab password.
unset POSTGRES_PASSWORD EXECUTOR_PROOF_PROVIDER
"${DC[@]}" exec api python -m cloudops.entrypoints.cli account operator --role operator
"${DC[@]}" exec api python -m cloudops.entrypoints.cli account viewer --role viewer
"${LAB[@]}" denials
"${LAB[@]}" containment
"${LAB[@]}" load --target both --seconds 90 --rate 10
"${LAB[@]}" verify
read -rs -p "Existing Operator password: " OPERATOR_PASSWORD
printf '%s\n' "$OPERATOR_PASSWORD" | "${LAB[@]}" collect --password-stdin
```

Fresh setup initializes the executor journal and a separate owned `executor_identity`
volume before starting application services. Preserve both authority volumes.
When upgrading an original unpinned v1 lab, stop its executor first and retain its
recorded deployment UUID. After building the current backend, use the same `DC`
array for explicit adoption, then retry `start`:

```sh
"${DC[@]}" run --rm -T --no-deps executor-init python -m cloudops.features.recovery.authority \
  --root /var/lib/cloudops-executor --identity-file /run/cloudops-executor-identity/identity.json \
  --adopt-legacy-id <ORIGINAL-DEPLOYMENT-UUID>
```

Startup refuses an existing journal without that confirmation. It never replaces
the journal, adopts an already pinned authority with a missing pin, or enables writes.

`collect` reads the existing Operator password as one stdin line; the example reads
it without terminal echo and keeps it out of arguments and generated files. It uses the normal
HTTPS API, admission and worker, checks content digests and compares raw logs using
the collector's captured window/options and exact Pod/container identities. The
first collection is retained as an immutable persistence baseline. `stored
--password-stdin` checks that baseline again without admitting another Run. Raw log
reads occur later than collection; comparison records differences and source
coverage without weakening completeness or inventing timestamps.

The endpoint is `https://localhost:8443`; Kubernetes is `127.0.0.1:6445`. Other
service ports are unpublished. CLI probes validate the copied CA. Browser trust is
separate: use the running Edge only if its existing trust permits this origin; do
not bypass warnings or change trust as a setup side effect.

Every `load` and `fault` requires fresh effective cgroup proof. The driver locates
sample processes beneath the limited K3s parent, checks memory/swap/CPU limits,
Pod controller/UID labels, node pressure, allocatable capacity and concurrency.
Kubelet reserves leave control-plane headroom. If actual allocatable capacity still
advertises unavailable host CPU/RAM, stress is blocked; never patch Node capacity.
The service envelope is at most 8.5 GiB and eight CPUs, including on-demand KRR and
load, with a 20-GiB disk budget, rotated logs and bounded Prometheus retention.
These are configured ceilings, not measured footprint or runtime qualification.
The disk budget requires monitoring available space; retention is not a filesystem quota.

For read-only resource-history preparation, use `containment --read-only`.
This retains the same effective cgroup, UID, node and headroom checks, but needs
only the reader and monitoring credentials. The HTTP `load` command uses this
read-only check too; an expired writer credential does not require renewing or
enabling write authority. Fault and recovery preparation keep their original
writer checks. Expired reader/monitoring credentials still block load.

### Finite resource-history recipe

`scripts/probe-resource-history.py` reuses the owned sample, load driver and
read-only report producer. It never starts the executor, renews a writer or
changes workload resources. Setup must first provide healthy zero-restart Pods
under the original Deployment UID/template, current reader/monitoring credentials,
the pinned KRR producer, effective observation retention and bounded runtime
containers. Fixture preparation is separate from the read-only recipe.
Readiness requires fresh unambiguous KSM metadata for each current Pod UID and
container, including ownership, creation, start and restart inputs. Missing
termination metrics are valid for zero-restart Pods. Source policy/configuration
digests remain bound across checkpoints and the final stored report; both live
Deployment reads must match the admitted UID/name/namespace.

Create a private mode-0600 JSON file with these fields:

| Field | Required value |
|---|---|
| `lab_root`, `owner` | Exact existing checkout path and UUID from its private `tmp/local-testing-lab/owner.json`; no resource adoption. |
| `worker_container` | Full 64-character ID of the owned worker with the actual reader, Prometheus and KRR configuration. |
| `runtime_containers` | Full IDs of the API, worker, observer, KRR and database used for the report; include any separately owned feature runtime. |
| `api_url`, `web_origin` | Actual loopback API origin and its configured web origin, without paths or credentials. |
| `operator`, `workload_id` | Existing Operator username and exact active saved workload UUID for the owned `demo/checkout` Deployment. |
| `ca_file` | Copied local CA path for HTTPS, or `null` for an explicitly owned loopback HTTP runtime. TLS verification is never disabled. |

```sh
uv run --project services/backend python scripts/probe-resource-history.py --config /private/history-lab.json
uv run --project services/backend python scripts/probe-resource-history.py --config /private/history-lab.json --short
```

The default is readiness only: no load or report admission. `--short` uses three
seconds of warmup, fifteen-second idle/steady/peak phases and at most ninety seconds
for checkpoint overhead and final peak hold/report handoff. Its 138-second cap begins after preflight.
The existing Operator password is prompted without echo; automation may supply
one stdin line with `--password-stdin`. A short result is always marked
`short_harness_only`, even if unrelated retained history happens to produce advice.

Only after the owning acceptance scope and runtime budget are settled, replace
`--short` with `--run` for the fixed real recipe: three minutes of warmup, nominal
two-hour idle/steady/peak phases and at most five minutes of final peak hold/report
handoff, capped at 6h08m. The recipe records actual phase times and query-window
intersections; it does not manufacture timestamps, replay arbitrary old history
or lower product coverage thresholds. Credential expiry must cover the whole cap
before starting. An invalid/missing source, changed UID/template/producer, restart,
budget failure or late/nonavailable report fails that cohort rather than extending
it or retrying admission. A queued report can finish later, but does not qualify
an expired cohort.

Before readiness or a fresh full run, add `--renew-readers` to the configured
`probe-resource-history.py` command (or use `manage-local-lab.py renew-readers`
from the updated owning checkout). This renews
only the existing reader and monitoring identities for their existing eight-hour
lifetime. It requires the owned source services already running, does not restart
services, and does not touch writer credentials or executor authority. Readers use
the renewed directory-mounted files on subsequent calls. Renew before the cohort;
the operation lock prevents overlapping lab operations during a run.

Receipts, raw timestamped measurements and the exact terminal export are private
under `tmp/local-testing-lab/resource-history/<cohort-id>/`. Each run takes the
existing operation lock. It removes only its own labelled temporary load container;
history, exports, database volumes, other containers and writer authority remain
intact. Retained sample images can be reused: the current bounded load source is
mounted read-only into the temporary load container and its hash is recorded.
The recipe reserves twenty seconds inside the cap for cleanup. The load process
receives an absolute deadline five seconds before the working deadline, so a
delayed container launch cannot start a new load after expiry.
Cleanup locates its cohort-labelled container even if launch acknowledgement was
lost. Read-only checkpoints have their own process, connection and statement bounds.

Faults affect only the recorded `demo/checkout` UID and condition every patch on its
current UID/resourceVersion. Run one fixed scenario, observe it, optionally collect
its evidence, then reset before another scenario:

```sh
"${LAB[@]}" fault crashloop   # alternatives: oom, traffic, pending, imagepull, init-retry
"${LAB[@]}" observe
printf '%s\n' "$OPERATOR_PASSWORD" | "${LAB[@]}" collect --password-stdin
"${LAB[@]}" reset
# For traffic, run bounded load before observe/collect:
"${LAB[@]}" fault traffic
"${LAB[@]}" load --target checkout --seconds 90 --rate 10
"${LAB[@]}" observe
printf '%s\n' "$OPERATOR_PASSWORD" | "${LAB[@]}" collect --password-stdin
"${LAB[@]}" reset
```

Observe requires an actual OOMKilled status, CrashLoop restart, Unschedulable
condition/event, runtime ImagePull error, or measured traffic error ratio, as
applicable. Each allocation is bounded; crash/init/OOM Pods continue retrying until
reset or stop. Reset restores only captured fault fields, preserves unrelated
metadata/environment and refuses concurrent field changes, active executions and
unresolved old writers. It cannot unlock a target or label reset as product recovery.
Previous-log gaps remain explicit and block complete CrashLoop/OOM evidence claims.

For same-name identity validation, load history, run `replace-history`, then load
both targets and verify again. Only the read-only history Deployment is replaced;
its old samples retain their old UID, reader scope is explicitly renewed, and writer
scope is unchanged. Stop/start and network recreation preserve persistent state:

```sh
"${LAB[@]}" stop
"${LAB[@]}" start
"${LAB[@]}" load --target both --seconds 90 --rate 10
"${LAB[@]}" verify
printf '%s\n' "$OPERATOR_PASSWORD" | "${LAB[@]}" stored --password-stdin
"${LAB[@]}" recreate
"${LAB[@]}" load --target both --seconds 90 --rate 10
"${LAB[@]}" verify
```

Stop closes dispatch and quarantines the executor. Start resumes collection/report
classes when needed, without enabling writer authority. Recreate replaces the K3s
container while keeping its data and explicitly **recreates** Prometheus in the new
network namespace. Fresh namespace attachment, DNS, discovery and authenticated TLS
kubelet scrapes must pass again. Monitoring shares no K3s files or PID namespace.
K3s may exit once when its persisted Node IP changes. Startup permits one restart
only for exit 1 with a fresh matching IP-change event, unchanged container identity
and no OOM. It checks the assigned Docker address, converged Node IP and stable
readiness; unrelated failures and a second failure remain errors.

Renew credentials explicitly with `renew reader`, `renew monitoring`, or `renew
writer`. Renewal can bring up the owned prerequisites after a stop; it does not
require an expired connection to authorize itself. Writer renewal requires quarantine and absence of unresolved execution;
it restarts the executor but does not enable it. A positive scale test first requires
fresh `containment`, then the documented exact-UID maintenance proof/enable/exit and,
when dispatch is in recovery, explicit resumption of `recovery_execution`. For that
explicit test, use the retained `DC` array and UID, rather than the legacy project
commands below:

```sh
TARGET_UID=$(uv run --project services/backend --frozen python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))[0]["uid"])' "$LAB_DIR/writer/workloads.json")
"${DC[@]}" exec -T executor python -m cloudops.features.recovery.maintenance enable_target \
  --target "$TARGET_UID" --reason "Explicit bounded scale acceptance" --evidence local://lab/containment
"${DC[@]}" exec -T executor python -m cloudops.features.recovery.maintenance exit_recovery_mode \
  --target "$TARGET_UID" --reason "Explicit bounded scale acceptance" --evidence local://lab/containment
"${DC[@]}" run --rm -T --no-deps dispatch-admin resume --class recovery_execution \
  --reason "Explicit bounded scale acceptance" --evidence local://lab/containment
```

The audit reference is not a substitute for proof: maintenance performs its own
conditional fence and current-state validation. Review/approve one fresh absolute
scale plan through the existing UI/API. An acknowledged patch still has manual,
inconclusive verification. After the test, use driver `stop` then `start` to close
write admission and leave the readable baseline available. The lab driver never
enables writes or automatically replays an approval.

Keep the safe baseline available at handoff. To deliberately delete it, first run
`stop`, read the non-secret owner UUID from `status`, then use `cleanup
--confirm-owner <UUID>`. Cleanup enumerates and validates only this project's
containers, network and volumes; it does not prune images or remove unrelated data.
The private directory and receipts remain for explicit local disposal. Removing the
cluster data is destructive and is not part of normal reset, stop or recreation.

| Testing need | Local entry point | Limit |
| --- | --- | --- |
| Accounts, HTTPS and authority separation | CLI accounts, `denials`, existing HTTPS probes | Browser trust requires its own authorized setup. |
| Kubernetes incident evidence | `fault`, `observe`, `collect`, `reset` | Check actual coverage; raw previous-log gaps remain gaps. |
| Measured metrics and identity history | `load`, `verify`, `replace-history` | Historical samples keep their original UID. |
| Persistence and source recovery | `stored`, `stop`/`start`, `recreate` | Restore drills use a separate disposable project and `_test` database. |
| Existing scale execution | Fresh containment, maintenance proof, explicit plan review/approval | Acknowledgement has manual, inconclusive verification. |
| Resource recommendation inputs | Real kubelet/KSM metrics and retained load history | Six hours alone does not qualify recommendations. |
| Diagnosis and future actions | Existing model-free fixtures and feature-specific tests | Installing the lab does not implement missing orchestration/actions or qualify a live provider. |

## Start the isolated lab

For optional UID-owned workload health, freshness and context, follow the [workload observer handoff](workload-observation.md). Its dedicated process has reader credentials only and does not occupy the collection/recovery worker. For a pinned real Alertmanager sending to intake, add the overlay described in [Alertmanager intake](alert-intake.md#local-lab). Set `CLOUDOPS_LAB_PROJECT` to run these scripts against a separate disposable Compose project.

Use Docker Desktop/Engine with Linux containers, Compose 2.24.4 or newer, and the README `.env` setup. The K3s server is a privileged **lab container**, not access to a host kubeconfig or an external cluster. All published endpoints remain on loopback. Host Python/uv are needed for the setup/probe scripts; runtime services use their locked container dependencies. The bootstrap script also issues the fresh-install [job dispatch permit](job-dispatch.md#trusted-administration) once and leaves an existing permit unchanged; collection, reports and execution dispatch wait until it exists.

```sh
uv run --project services/backend python scripts/bootstrap-integration.py
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml up -d --build --wait --wait-timeout 240 api worker executor krr https
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -m cloudops.entrypoints.cli account operator --role operator
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -m cloudops.entrypoints.cli account viewer --role viewer
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml cp https:/data/caddy/pki/authorities/local/root.crt tmp/local-https-root.crt
```

The bootstrap applies `config/lab/workload.yaml` only to its own K3s service, waits for `demo/checkout`, captures its actual UID and creates separate eight-hour ServiceAccount tokens under ignored `tmp/e01-integration`. It never prints token values. Refresh credentials by rerunning setup with product writes quarantined, synchronizing workloads through the CLI and restarting the executor; restart always requires new enable/exit proof. Changing a Deployment UID preserves the prior application identity as inactive history.

The API mounts the read-only observation connection. The worker additionally mounts private dispatch/KRR request keys. Only the isolated executor mounts the Kubernetes writer connection and maintenance socket; neither application container mounts them or a Docker socket. Kubernetes RBAC permits that writer to patch only the named Deployment in `demo`; reader/writer accounts cannot read Secrets. Capacity checks conservatively support the lab's single schedulable node. Other placement/controller cases are denied for manual review.

The fresh-install bootstrap explicitly runs `executor-init` once before starting the executor. Its external identity pin resides in `executor_identity`, separately from `executor_authority` and PostgreSQL. The pin records the deployment ID and actual journal/lock file identities. Preserve both volumes; missing or replaced authority is never initialized automatically by the serving gateway. The executor starts in independent recovery mode with every target disabled, including after a container restart. Read the UID from `tmp/e01-integration/writer/workloads.json`, then use the real deployment-root channel:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance status
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance enable_target --target <UID> --reason "Initial local readiness" --evidence "local://operator/current-state"
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec executor python -m cloudops.features.recovery.maintenance exit_recovery_mode --target <UID> --reason "Explicitly enable the reconciled lab" --evidence "local://operator/current-state"
```

The evidence strings are audit references, not the safety proof. The executor itself writes a unique nonfunctional annotation through an atomic UID/resourceVersion-tested patch and reads it back with current health. Application roles have no maintenance endpoint or override. The Unix server obtains UID/PID with `SO_PEERCRED`; no caller-supplied actor is accepted. The recorded actor is isolated deployment-root, UID/PID and deployment ID, not an invented individual operator identity. Non-root peers receive a durable denial. Every mutation needs a reason, evidence references and exact configured targets; global recovery operations require all configured targets.

Open `https://localhost:8443` after accepting this lab's CA through normal local certificate setup. The request probes take `--ca` and validate the real certificate chain and hostname; they never disable TLS validation. Browser trust is a separate workstation choice. Remove any temporary trust added solely for testing after the browser exercise; preserve pre-existing trust.

## Consumer behavior

Versioned name-based live identity reconciliation, failure semantics and its read-only UI are documented in [Host-configured workload scope](workload-scope.md). The bootstrap's legacy UID-pinned representation remains supported; do not overwrite an approved v2 scope with freshly generated legacy files.

- Workloads expose selected image/resource baseline candidates from two matching healthy observations at least five seconds apart, valid for five minutes. Confirmation saves UID/digest/actor/time metadata and never patches Kubernetes. Referenced configuration and Secrets are explicitly outside the snapshot.
- Incident notes and acknowledge/resolve/reopen use the existing principal. Resolution captures an immutable stored handoff; later notes/reopen cannot rewrite that export. Queued/executing recovery blocks resolution and reinvestigation. An unknown execution may remain visible in a resolved incident and retains its lock until trusted proof.
- Connection checks have fixed `cluster`, `database` and `worker` destinations. Results are persisted observations; no request accepts an endpoint or credential.
- KRR runs the pinned 1.31.1 tool against the configured read-only cluster and Prometheus. The lab has no representative container history. Its actual report is `insufficient_data`, with tool exit/output digest and null coverage/recommendations; no savings or apply result is fabricated. E08 still owns qualified idle/steady/peak history and recommendation quality.
- Optional scoped Prometheus metric evidence uses the `compose.metrics.yaml` overlay, a labeled metrics fixture and the Caddy `/prometheus/*` TLS route. Start the HTTPS proxy before writing the read config, then start API/worker as described in [scoped Prometheus metric evidence](prometheus-metrics.md#local-lab). The default lab remains fixture-only; the fixture is not a product app exporter.
- Recovery currently supports only Operator-proposed `scale_deployment`, absolute replicas 1–3. The immutable plan contains source/UID/current resourceVersion/selected before fields, rationale, digest, target epoch and five-minute expiry. Live UID/controller/capacity checks run at preview, decision and dispatch. One durable target lock and a thirty-second admission cooldown prevent overlapping/repeated changes. Other recovery templates remain unavailable.
- Reading a plan returns live validity and a receipt tied to the current session, expiring within sixty seconds. Approval/rejection requires its exact version/digest and a fresh explicit action. Reauthentication invalidates old receipts. The UI binds the confirmation dialog to the reviewed plan and discards intent when authority or the displayed plan changes. No mutation is replayed automatically.
- An acknowledged Kubernetes patch is **not** verified remediation. Results remain `manual_intervention`/`inconclusive`; rollout, application metrics and any subsequent change require manual assessment. There is no automatic rollback.

Viewers read/export these records and log out their own session. All nine Operator mutation operations reject a valid Viewer request before any committed domain/job change. Anonymous/expired requests cannot read observations, review receipts or stored records. The current operation inventory and executable role cases, including the additional scope/history reads, are in `services/backend/tests`; provider fixtures are explicitly separate from the live probes.

## Writer and maintenance protocol

The private executor serializes admission with a Linux file lock and journals the exact attempt before sending a conditional patch. Exact retries retrieve a durable outcome instead of writing again; changed content under an old execution ID is rejected. All functional patches test both UID and the captured resourceVersion. The existing v1 worker still replaces an absolute replica count and retains its original wire/receipt format. A lost acknowledgement keeps `outcome_unknown` and quarantine.

The gateway also accepts the closed `RecoveryDispatch/v2` forward/inverse union for selected image/resource or scale changes. It requires the exact approved plan/digest, policy identity and claimed active Execution, then independently checks original UID, selected fields, controllers, baseline/profile or captured inverse qualification and candidate capacity. It builds the patch itself. New R1 preview records have an unavailable execution capability and cannot gain authority retroactively; the product path remains gated until the matching lifecycle/qualified verification is installed. Protocol fixtures do not enable this product path.

V2 capacity is conservative: retain current allocations, including terminating Pods, and reserve the full desired candidate template plus rollout surge. Memory/restore do not use the legacy replica-delta shortcut. Supported placement remains one ready schedulable node. This proves bounded declared requests at observation time; it does not promise that a competing writer, later scheduling change or application load cannot invalidate recovery. The supported deployment requires the one pinned journal/lock and isolated credential topology; invisible out-of-protocol writers are outside that proof.

An inverse names the exact durable acknowledged forward operation/plan and swaps its captured selected values. It never takes arbitrary old values or requires a new forward baseline/profile selector. Template/memory inverses additionally match a separately qualified original-UID/container state in the trusted policy's `inverse_qualified_states` (`containers` plus `qualification_reference`); unsafe or missing old-state qualification denies. One inverse may be admitted per forward operation. Read-only authenticated `GET /operations/{operation_id}` returns the durable receipt; missing receipt or matching current desired fields does not establish no write or authorize a resend.

For an original **unpinned v1 journal only**, stop the serving executor and run the initialization helper with its recorded original deployment ID using the same authority/identity mounts:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml run --rm --no-deps executor-init python -m cloudops.features.recovery.authority --root /var/lib/cloudops-executor --identity-file /run/cloudops-executor-identity/identity.json --adopt-legacy-id <ORIGINAL-DEPLOYMENT-UUID>
```

Use the unmodified original journal, not an application dump or copied replacement. An already pinned authority with a lost pin cannot be adopted as legacy: recover its original identity/journal or keep admission closed. Initialization grants no target enablement and is never old-writer exclusion proof; the actual fence/readback and explicit enable/exit steps remain required.

The serving gateway alone receives its Kubernetes writer credential. Its RBAC needs `get`/`patch` on explicitly named Deployments (including the nonfunctional fence), cluster read/list for Nodes/Pods to prove capacity, and namespaced read/list for HPA/VPA discovery. It needs no Secret-content, namespace, Node or arbitrary workload write. Kubernetes RBAC does not restrict individual JSON Patch fields; the closed gateway validator enforces that boundary. App/observer processes receive scoped reader credentials, and only the worker receives the private gateway dispatch key. Neither app roles nor a dispatch key grant access to the deployment-root maintenance socket.

Maintenance closes admission and retires the target epoch first. Already queued old epochs are rejected. Its acknowledged annotation fence changes the resourceVersion; read-back and explicit inequality with captured unresolved versions exclude already admitted late patches through the Kubernetes atomic precondition. ResourceVersions are compared for equality only, never numerically ordered. A five-second health observation bound is only a readiness check, not old-writer proof; neither elapsed time nor an expired worker lease proves exclusion.

`release_uncertain_target` can remove the application safety lock only after proof, retaining historical `outcome_unknown` and leaving the target disabled. `enable_target` and global `exit_recovery_mode` are separate explicit proof-backed operations. A durable `maintenance_released` marker prevents late worker publication from relabeling or relocking the historical outcome. SQLite attribution/exclusion/audit history resides in the separate executor authority volume, outside application PostgreSQL. Status exposes retained unknown attribution even across application restore. An unavailable proof provider denies enable/release/exit.

Use `--request-id <UUID>` to retrieve an exact maintenance request's durable receipt without repeating it. A replay includes current target states; historical success does not re-enable a target after restart. Preserve the local authority volume and single-host shared-lock topology. Replacing its storage or moving to another topology requires deployment-owner reconciliation, not copying a PostgreSQL lock bit or deleting the journal.

## Restore and restart

1. Enter recovery through the deployment-root CLI, covering every target; stop API and worker before restoring application data. Preserve the executor authority volume and Kubernetes identity.
2. Restore the known application backup, then run the current image's `cloudops.entrypoints.cli revoke-all-sessions` **before starting the API**. This removes sessions resurrected by an older database snapshot without hand-editing rows. Apply required migrations and synchronize scope through the CLI.
3. Restart the executor/API in independent quarantine. Inspect actual target state, retained unknown attribution and root audit receipts. Do not infer readiness from restored application rows.
4. Establish current UID/fence/health proof, explicitly enable each target, explicitly exit recovery mode, and only then resume workers/product writes. Users sign in and read current records/plans again. Previously admitted old-epoch work cannot authorize a new write.

Migration `0003_e01_consumers` adds eight feature tables plus a source column; populated upgrade preserves existing foundation records and assigns old workloads `development_fixture`. Repeat upgrade and metadata drift checks are tested. Downgrade removes new feature data and does not undo lifecycle values or Kubernetes effects; use a quarantined backup recovery rather than an application rollback assumption.

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
