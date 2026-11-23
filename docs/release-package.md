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
python3 "$verifier" verify "$bundle"
mkdir /path/to/cloudops-install
tar -xf "$bundle/source.tar" -C /path/to/cloudops-install
cd /path/to/cloudops-install
docker image load --input "$bundle/images.tar"
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
python3 "$verifier" verify "$bundle" --loaded
compose up -d --no-build --pull never --wait --wait-timeout 120 api worker https
# First install only. Account provisioning resets credentials and revokes prior sessions.
compose exec api python -m cloudops.entrypoints.cli account operator --role operator
compose exec api python -m cloudops.entrypoints.cli account viewer --role viewer
compose ps
# Stop the selected project while retaining its database, permit and TLS volumes.
compose stop
```

The release overlay selects collection mode `none`. The application can show unknown or unavailable sources without returning synthetic evidence. Job dispatch starts denied on a fresh project unless a trusted deployment operator explicitly installs a permit. The core package includes no executor, model credential, K3s scope or live KRR producer. Use the [owned lab integration](e01-local-lab.md) for the scoped source and writer topology. Its credentials, sample and profile qualification, application gates and restore proof are separate from the core installation. Never enable a writer merely because a compiled release starts.

For a populated upgrade of this core installation, stage and verify the new bundle and loaded image IDs first, preserve the current backup and external authorities per [restore and restart](e01-local-lab.md#restore-and-restart), stop old consumers, and use the same project and volumes. Verify migration and readiness before starting connected work. Core bootstrap syncs the development scope. Use the scoped lab procedure for an existing live-scope database. Keep the prior artifact identities and retained data. A schema rollback requires the supported restore sequence. Record the installed bundle, image IDs and current migration head. Use the existing [HTTPS smoke](foundation.md#local-https-exercise) for the release-path session check without changing certificate trust automatically.
