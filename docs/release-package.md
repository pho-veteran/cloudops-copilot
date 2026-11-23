# Pinned HTTPS application package

Build on a trusted Linux/amd64 Docker host before transferring the release to the target. Use Python 3.12 or later and Docker Compose 2.24.4 or later. The bundle contains the exact reviewed source archive, the FastAPI/worker image, the compiled web/Caddy image and PostgreSQL. The image build resolves registry digests from the existing Dockerfiles and Compose configuration and uses the existing Python and Node locks. The manifest records the revision, the selected upstream digests, local image identities and artifact SHA-256 values. Preserve that manifest with the artifacts for subsequent installs. A new build may select new upstream digests and needs a new review. Image aliases include both the source revision and content ID.

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
  python3 "$verifier" verify "$bundle" --loaded
```

## Start and stop

Create a private `host.env` with a URL-safe `POSTGRES_PASSWORD` for this project. Choose an unused Compose project for a first install. Use that same project and its core installation volume across later starts, upgrades and stops. Unset inherited `RELEASE_BACKEND_IMAGE`, `RELEASE_DB_IMAGE` and `RELEASE_HTTPS_IMAGE` variables before using the bundle's environment file. Start only the core scope below. The only published listener is loopback `https://localhost:8443`. Keep the exact origin, with approved trust for its local CA. For remote transport, use the exact loopback origin through the authorized management connection.

```sh
release_project=cloudops-release-OWNED_NAME
unset RELEASE_BACKEND_IMAGE RELEASE_DB_IMAGE RELEASE_HTTPS_IMAGE
compose() {
  docker compose -p "$release_project" --env-file ./host.env --env-file "$bundle/images.env" \
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
| Images | Exactly three runtime images from `images.tar`: backend (API, worker, migration, observer, dispatch administration), compiled web with Caddy, and PostgreSQL 17.11. The scoped owned lab separately pins PostgreSQL 17.12. Cross-lab restores stay within major version 17. |
| Storage | Keep the bundle, install directory, Docker images and project volumes on a disk with at least 20 GiB free after loading. Volumes are `database`, `job_dispatch`, `local_https_data` and `local_https_config`. |

Measured sizes, idle use and the qualified environment for a specific candidate are recorded in the [release package evidence](evidence/e10-s1-release/README.md).

## Upgrade a populated core project

Run each step with the same `release_project` and `host.env`. Stage the new bundle while the current release still serves.

```sh
new=/path/to/cloudops-NEW_REVISION
python3 "$verifier" verify "$new" &&
  docker image load --input "$new/images.tar" &&
  python3 "$verifier" verify "$new" --loaded &&
  mkdir /path/to/cloudops-install-NEW &&
  tar -xf "$new/source.tar" -C /path/to/cloudops-install-NEW
```

From the current install directory, back up the database to private storage. If a dispatch permit exists, close dispatch first. Then stop the consumers.

```sh
compose exec -T db pg_dump -U cloudops -d cloudops -Fc > /path/to/private/before-upgrade.dump
compose run --rm dispatch-admin enter-recovery --reason "Upgrade to NEW_REVISION" --evidence "local://operator/upgrade"
compose stop worker api https
```

From the new install directory, select the new bundle in the same `compose` function and start it. Migration runs before the API and worker start. A repeated start applies no further migration.

```sh
bundle="$new"
compose up -d --no-build --pull never --wait --wait-timeout 120 api worker https
compose exec -T api alembic -c services/backend/alembic.ini current
```

Check readiness, the HTTPS smoke and saved records. Then resume dispatch explicitly with `exit-recovery` and the same reason/evidence form. Keep the old bundle and backup until the new release is accepted.

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

Generated credentials stay under the install directory's ignored `tmp/e01-integration`. The executor starts in recovery mode with every target disabled. KRR, Prometheus and the retained owned testing lab are outside the bundle, and the owned lab refuses these variables.

## Dependency advisories

Each candidate records fresh locked npm, Python and image advisory results with applicability and disposition in its release evidence. A remaining development-only advisory must be absent from the runtime images and the compiled web output, and must name its owner and release gate.
