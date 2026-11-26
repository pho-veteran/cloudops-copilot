# Pinned HTTPS application package

Build on a trusted Linux/amd64 Docker host before transferring the release to the target. Use Python 3.12 or later and Docker Compose 2.24.4 or later. The bundle contains the exact reviewed source archive, the FastAPI/worker image, the compiled web/Caddy image and PostgreSQL. A version 2 bundle also carries `lab-images.tar` and `lab-images.env` with the owned lab images built from the same revision: the KRR runner, the measured sample and the healthy and bad-image recovery sample variants. The image build resolves registry digests from the existing Dockerfiles and Compose configuration and uses the existing Python and Node locks. The manifest records the revision, the selected upstream digests, local image identities and artifact SHA-256 values. Preserve that manifest with the artifacts for subsequent installs. A new build may select new upstream digests and needs a new review. Image aliases include both the source revision and content ID.

```sh
revision=$(git rev-parse HEAD)
python3 scripts/release-bundle.py build --revision "$revision" --output "/path/to/cloudops-$revision"
```

Transfer the bundle and compare its manifest with the copy from the trusted builder. Use the verifier from a trusted checkout of this tooling to check all bundle files before extracting or running any bundled code. Keep the bundle outside the runtime project directory and unpack it into a new, empty directory. Keep host credentials out of the bundle, images and Docker build arguments.

```sh
bundle=/path/to/cloudops-BUNDLE_REVISION
verifier=/path/to/trusted-checkout/scripts/release-bundle.py
python3 "$verifier" verify "$bundle" &&
  mkdir /path/to/cloudops-install &&
  tar -xf "$bundle/source.tar" -C /path/to/cloudops-install &&
  cd /path/to/cloudops-install &&
  docker image load --input "$bundle/images.tar" &&
  docker image load --input "$bundle/lab-images.tar" &&
  python3 "$verifier" verify "$bundle" --loaded
```

`verify --loaded` checks every image group in the manifest, so load `lab-images.tar` before it for a version 2 bundle. A version 1 bundle holds the core images only.

## Start and stop

Create a private `host.env` with a URL-safe `POSTGRES_PASSWORD` for this project, outside every install directory so later upgrades reuse it. Choose an unused Compose project for a first install. Use that same project and its core installation volume across later starts, upgrades and stops. Unset inherited `RELEASE_BACKEND_IMAGE`, `RELEASE_DB_IMAGE` and `RELEASE_HTTPS_IMAGE` variables before using the bundle's environment file. Start only the core scope below. The only published listener is loopback `https://localhost:8443`. Keep the exact origin, with approved trust for its local CA. For remote transport, use the exact loopback origin through the authorized management connection.

```sh
release_project=cloudops-release-OWNED_NAME
host_env=/path/to/private/host.env
unset RELEASE_BACKEND_IMAGE RELEASE_DB_IMAGE RELEASE_HTTPS_IMAGE
compose() {
  docker compose -p "$release_project" --env-file "$host_env" --env-file "$bundle/images.env" \
    -f compose.yaml -f compose.https.yaml -f compose.release.yaml "$@"
}
python3 "$verifier" verify "$bundle" --loaded &&
  compose up -d --no-build --pull never --wait --wait-timeout 120 api worker https &&
  compose ps
```

On the first install, provision the accounts. Each provisioning command resets that account's credentials and revokes prior sessions.

```sh
compose exec api python -m cloudops.entrypoints.cli account operator --role operator
compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
```

When work is finished, stop this project and retain its database, permit and TLS volumes.

```sh
compose stop
```

The release overlay selects collection mode `none`. The application can show unknown or unavailable sources without returning synthetic evidence. Job dispatch starts denied on a fresh project unless a trusted deployment operator explicitly installs a permit. The core package includes no executor, model credential, K3s scope or live KRR producer. Use the [owned lab integration](e01-local-lab.md) for the scoped source and writer topology. Its credentials, sample and profile qualification, application gates and restore proof are separate from the core installation. Never enable a writer merely because a compiled release starts.

For a populated upgrade of this core installation, stage and verify the new bundle and loaded image IDs first, preserve the current backup and external authorities per [restore and restart](e01-local-lab.md#restore-and-restart), stop old consumers, and use the same project and volumes. Verify migration and readiness before starting connected work. Core bootstrap syncs the development scope. Use the scoped lab procedure for an existing live-scope database. Keep the prior artifact identities and retained data. A schema rollback requires the supported restore sequence. Record the installed bundle, image IDs and current migration head. Use the existing [HTTPS smoke](foundation.md#local-https-exercise) for the release-path session check without changing certificate trust automatically.

## Requirements

| Item | Requirement |
| --- | --- |
| Architecture and OS | Linux/amd64 Docker Engine with cgroup v2 for the builder and the target. The bundle rejects other image platforms. |
| Tools | Docker Compose 2.24.4 or later and Python 3.12 or later on both hosts. The builder also needs Git and Docker Buildx with registry access. The target needs no registry, Git or Node. |
| Network | The core project publishes only `127.0.0.1:8443`. API, PostgreSQL and worker ports stay unpublished. Reach the origin locally or through the authorized management tunnel. |
| Images | Exactly three runtime images from `images.tar`: backend (API, worker, migration, observer, dispatch administration), compiled web with Caddy, and PostgreSQL 17.12, the same minor version as the scoped owned lab. A version 2 bundle adds four lab images in `lab-images.tar` for the owned recovery lab only. |
| Storage | Keep the bundle, install directory, Docker images and project volumes on a disk with at least 20 GiB free after loading. Volumes are `database`, `job_dispatch`, `local_https_data` and `local_https_config`. |

Measured sizes, idle use and the qualified environment for a specific candidate are recorded in the [release package evidence](evidence/e10-s1-release/README.md).

## Upgrade a populated core project

Run each step with the same `release_project` and `host_env`. Stage the new bundle while the current release still serves.

```sh
new=/path/to/cloudops-NEW_REVISION
python3 "$verifier" verify "$new" &&
  docker image load --input "$new/images.tar" &&
  python3 "$verifier" verify "$new" --loaded &&
  mkdir /path/to/cloudops-install-NEW &&
  tar -xf "$new/source.tar" -C /path/to/cloudops-install-NEW
```

From the current install directory, record the dispatch state, close dispatch if a permit exists and stop the consumers. For a restore from backup rather than an upgrade, follow [release backup and restore](release-restore.md). Then back up the database, which is still running, to private storage.

```sh
compose run --rm -T dispatch-admin status > /path/to/private/dispatch-before-upgrade.json
compose run --rm dispatch-admin enter-recovery --reason "Upgrade to NEW_REVISION" --evidence "local://operator/upgrade"
compose stop worker api https
compose exec -T db pg_dump -U cloudops -d cloudops -Fc > /path/to/private/before-upgrade.dump
```

From the new install directory, select the new bundle in the same `compose` function and start it. Migration runs before the API and worker start. A repeated start applies no further migration.

```sh
bundle="$new"
compose up -d --no-build --pull never --wait --wait-timeout 120 api worker https
compose exec -T api alembic -c services/backend/alembic.ini current
```

Check readiness, the HTTPS smoke and saved records. Then restore the recorded dispatch state explicitly with the same reason/evidence form. If it was `normal`, use `exit-recovery`. If it was already `recovery`, use `resume --class` only for each class it allowed, and never open a class that was closed. Keep the old bundle and backup until the new release is accepted.

## Restart and stop checks

`compose stop` keeps containers and every project volume. After `compose up` with the same bundle, check the following:

- Containers run the manifest image IDs.
- The migration head is unchanged.
- The HTTPS root certificate matches the one already trusted.
- The dispatch permit and recovery state are as they were before the stop.
- Saved incidents and runs are unchanged.

Never use `down --volumes` on a retained release project.

## Scoped lab with release images

Use the disposable [E01 integration lab](e01-local-lab.md#start-the-isolated-lab) from an install directory to qualify reader, monitoring and writer separation with the bundle images. Stop the core project first because both use port 8443. Use a separate project and private `host.env`. `compose.release-lab.yaml` runs the executor and its initialization from the bundle backend image and selects `live_kubernetes` collection. The lab scripts add the release overlays when these variables are set:

```sh
umask 077
export CLOUDOPS_LAB_PROJECT=cloudops-release-lab-OWNED_NAME
export CLOUDOPS_RELEASE_BUNDLE="$bundle" CLOUDOPS_HOST_ENV=/path/to/private/host-lab.env
unset RELEASE_BACKEND_IMAGE RELEASE_DB_IMAGE RELEASE_HTTPS_IMAGE
lab() {
  docker compose -p "$CLOUDOPS_LAB_PROJECT" --env-file "$CLOUDOPS_HOST_ENV" --env-file "$bundle/images.env" \
    -f compose.yaml -f compose.https.yaml -f compose.integration.yaml \
    -f compose.release.yaml -f compose.release-lab.yaml "$@"
}
uv run --project services/backend python scripts/bootstrap-integration.py
lab up -d --no-build --pull never --wait --wait-timeout 240 api worker executor https
uv run --project services/backend python scripts/probe-lab-credentials.py
```

Generated credentials stay owner-only under the install directory's ignored `tmp/e01-integration`; the scripts also apply `umask 077` in release mode. The scripts refuse release mode without an explicit new `CLOUDOPS_LAB_PROJECT`, for the default or retained lab projects, or with an inherited `RELEASE_BACKEND_IMAGE`, `RELEASE_DB_IMAGE` or `RELEASE_HTTPS_IMAGE`. This disposable release lab omits the Alertmanager overlay, whose container cannot read owner-only files. The executor starts in recovery mode with every target disabled. Use the owned recovery lab below for alert intake, Prometheus, KRR and recovery scenarios from the same bundle. `manage-local-lab.py` exits before any change when `CLOUDOPS_RELEASE_BUNDLE` is set for a retained source-built lab project.

## Owned recovery lab with release images

`manage-local-lab.py` runs a new owned recovery lab from a verified version 2 bundle when `CLOUDOPS_LAB_PROJECT` is `cloudops-release-recovery-NAME` (1-24 lowercase letters or digits) and `CLOUDOPS_RELEASE_BUNDLE` names the bundle. It is the E10 recovery lab with the same Compose settings, limits, credentials, containment and guards, and only the image sources change:

- `compose.lab-release.yaml` is added last. It runs the API, worker, observer, executor, migration, dispatch administration and test services from the bundle backend image, the HTTPS proxy and PostgreSQL from the bundle, and KRR from `lab-images.env`, all with `pull_policy: never` and no build.
- `start` builds nothing. It checks that every loaded bundle image has the manifest ID, `linux/amd64` and, for built images, the manifest revision label. It then points the lab's source-hashed sample and recovery tags at the bundle sample and recovery images and imports them into the owned K3s node. It refuses when such a tag already names a different image.
- The first `start` binds the lab marker to the bundle revision and manifest SHA-256. Later `start` and `resume` refuse a different bundle or a changed `images.env` or `lab-images.env`; install a new bundle into a new project.
- K3s, Prometheus, Alertmanager and kube-state-metrics stay pinned by digest in the reviewed Compose and manifest files. Start verifies those registry digests and pulls by digest; K3s still pulls its own pinned system images.
- Alertmanager keeps the lab's root user with only `DAC_OVERRIDE`, so it reads the owner-only sender token and CA copy.
- The project directory is `tmp/cloudops-release-recovery-NAME` under the install directory. Retained source-built projects, inherited `RELEASE_*` image variables and `CLOUDOPS_RELEASE_RESTORE` are refused.

From the install directory, after `verify --loaded`:

```sh
umask 077
export CLOUDOPS_LAB_PROJECT=cloudops-release-recovery-NAME CLOUDOPS_RELEASE_BUNDLE="$bundle"
unset RELEASE_BACKEND_IMAGE RELEASE_DB_IMAGE RELEASE_HTTPS_IMAGE RELEASE_KRR_IMAGE \
  RELEASE_SAMPLE_IMAGE RELEASE_RECOVERY_HEALTHY_IMAGE RELEASE_RECOVERY_BAD_IMAGE_IMAGE
lab="uv run --project services/backend --frozen python scripts/manage-local-lab.py --project $CLOUDOPS_LAB_PROJECT"
$lab preflight
$lab start
$lab recovery-config
```

Then follow the [E10 S3 recovery lab](evidence/e10-s3/README.md#recovery-qualification) qualification and trial flow and the [S6 campaign](evaluation-baseline.md) with this project selected. `scripts/release_scenarios.py` accepts recovery reset receipts from this project, and the S6 campaign binds the project selected by `CLOUDOPS_LAB_PROJECT`. The core and disposable release labs above are unchanged.

Before freezing a diagnosis campaign, use the same private lab Compose configuration
to run the `migrate` service as a trusted maintenance process with
`python -m cloudops.entrypoints.cli knowledge-index --root /workspace --json`.
Its reviewed corpus is mounted read-only at `/workspace/runbooks`. Check
`knowledge-status --json` for `ready` with catalog search and retrieval available,
and retain the secret-free result. The API and worker do not index at startup.

Also collect current and previous logs during a separate scenario calibration. A
Kubelet response containing `unable to retrieve container logs for ...` can arrive
with HTTP 200; it is a runtime source failure and is classified as unavailable, not
application log evidence. A missing required source continues to block its affected
assessment/scoring. Retain the runtime limitation and resolve it before declaring a
new cohort ready; do not waive it or rewrite an earlier captured result.

## Optional local diagnosis gateway

On Docker Desktop, compose `compose.diagnosis.yaml` followed by
`compose.diagnosis-gateway.yaml` to reach an owner-run Windows gateway that listens
only on `127.0.0.1:20128`. Verify reachability on the actual host before qualification;
this route is not portable to an arbitrary Linux host or EC2. Keep the profile URL
`http://localhost:20128/v1` with explicit `allow_insecure_loopback: true`.

The digest-pinned, unprivileged forwarder publishes no ports, receives no mounts or
credentials and listens only on its network namespace's loopback. Workers, including
`compose run --rm` one-shot workers, join that namespace. The API stays on the ordinary
network and receives only the secret-free diagnosis configuration. Inspect actual
environment and mounts before live work. Keep the ordinary worker stopped during S6;
start only `diagnosis-gateway` and the finite worker phases. Stop the forwarder after
the campaign. Load or pull its exact image digest separately and retain its image ID
and advisory result with the candidate; it is not in the core or lab image archives.

For a WSL NAT qualification driver, a private WSL-loopback relay can bridge through
Docker Desktop to `host.docker.internal:20128` without publishing a host port. Record
the relay's source hash, pinned forwarder image and listener inspection. Do not rebind
the Windows gateway to a public interface or change firewall/profile trust to make it
reachable. The S6 source manifest binds the reviewed Compose overlay.

Endpoint compatibility alone does not qualify diagnosis. A model combo must keep its
qualified member set and routing strategy; changing it requires new qualification.

## Dependency advisories

Each candidate records fresh locked npm, Python and image advisory results with applicability and disposition in its release evidence. A remaining development-only advisory must be absent from the runtime images and the compiled web output, and must name its owner and release gate.
