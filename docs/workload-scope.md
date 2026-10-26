# Host-configured workload scope

Workload synchronization resolves an operator-approved name allowlist to immutable Kubernetes Deployment UIDs. It writes application metadata only. It does not enable an executor, copy baselines to a replacement, patch Kubernetes or establish workload health. See [the local lab handoff](e01-local-lab.md) for existing reader/writer separation and [E02 #5](https://github.com/pho-veteran/cloudops-copilot/issues/5) for feature acceptance.

## Existing UID-pinned configuration

The existing list format remains valid, including development fixtures and explicitly UID-pinned live entries:

```json
[
  {
    "cluster": "local-k3s",
    "namespace": "demo",
    "name": "checkout",
    "uid": "the-explicitly-pinned-live-uid",
    "source": "live_kubernetes"
  }
]
```

Legacy synchronization retains its configured-UID authority; it does not perform name discovery. A fixture never becomes live merely because the application can reach a cluster. Do not mix legacy entries and versioned live policy in one document. The local lab bootstrap continues to produce the legacy representation for reader and writer separately.

## Opt into versioned live scope

Provision the reader `client.json` through the trusted deployment channel. It contains one HTTPS server, one base64-encoded PEM CA certificate and the reader token. Only the expected server/CA identity is placed in the scope policy; credentials remain in the private reader file.

Inspect the fingerprint of that trusted connection explicitly, for example inside the lab API container:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -c 'import json; from cloudops.features.workloads.scope_config import cluster_fingerprint; print(cluster_fingerprint(json.load(open("/run/cloudops-reader/client.json"))))'
```

The command prints only a fingerprint. Independently establish that the reader configuration identifies the intended deployment before approving it. Normal synchronization never generates or updates the expected fingerprint. A value obtained from an untrusted replacement connection is not proof of continuity.

The algorithm is `SHA256(normalized_server + "\n" + SHA256(CA_DER).hexdigest())`. The normalized HTTPS URL has a lowercase host, an explicit effective port and no trailing root slash. Credentials, query/fragment and non-root paths are rejected. The CA must decode to exactly one valid PEM certificate; bundles and extra material are rejected. Hashing DER makes PEM line wrapping irrelevant.

Replace the reader workload policy atomically with:

```json
{
  "schema_version": 2,
  "cluster": {
    "key": "local-k3s",
    "fingerprint_algorithm": "server-ca-sha256/v1",
    "expected_fingerprint": "<the independently approved 64-character lowercase fingerprint>"
  },
  "deployments": [
    { "namespace": "demo", "name": "checkout" }
  ]
}
```

V2 accepts at most 100 unique Deployment names in one cluster. Preserve the existing cluster key. Matching legacy live key/UID retains the workload UUID and its history. A cluster-key rename is rejected; it is not a way to migrate identities. Once v2 is applied, an ordinary sync cannot downgrade to legacy authority. Do not rerun the legacy bootstrap over an active v2 policy without reconciling its output first. Keep the writer allowlist UID-pinned and unchanged.

Startup `bootstrap` migrates then synchronizes before API/worker startup. For an explicit host synchronization:

```sh
docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml exec api python -m cloudops.entrypoints.cli sync-workloads
```

No product endpoint performs this operation. A Viewer or Operator browser session grants no host-policy privileges.

## Outcomes and recovery

| Outcome | Application behavior | Operator action |
|---|---|---|
| Same UID | Keep UUID/history and current admission | No identity migration needed |
| New UID | Retire the old identity, create/reuse the exact new identity; no inherited baseline or writer enablement | Inspect the replacement; writer maintenance remains separate |
| Successful 404 | Mark the configured target missing; close old identity admission, preserve history | Correct the name or recreate deliberately, then sync |
| Removed from valid policy | Close admission, retain saved records | Read history through existing incidents or saved identity inspection |
| 403/timeout/transport failure | Preserve prior identity and admission; publish unavailable with a classified reason | Restore the reader/connection and sync again |
| Never resolved | Show a configured target with no workload UUID/UID | Investigation/baseline preparation is unavailable until resolution |
| Wrong server/CA pin or invalid policy | Fail without publishing any identity/admission changes | Reconcile the trusted host configuration; do not remove the pin |
| Superseded/config changed | Discard the staged reads | Run a new explicit sync after coordinated configuration changes |

The CLI exits nonzero for invalid policy, binding conflict and superseded publication. A valid partial batch exits zero with explicit unavailable counts and a stderr warning: this permits serving prior identities during reader outages, and is not a claim that all targets were observed successfully. Monitor the counts/status rather than exit code alone. Missing and unavailable remain distinct.

Resolution uses a 60-second scheduling budget, per-request timeouts and a 2 MB response cap. No further target request starts after budget exhaustion. Remaining targets are unavailable, never fabricated as missing. Network reads do not hold a database transaction. A database sequence fences publication across CLI processes, including after a newer process fails. An interrupted process cannot regain publication authority; retry explicitly. Coordinate scope file changes and use atomic replacement. Database revision checks do not make filesystem edits transactional with PostgreSQL.

The Workloads screen shows versioned scope status separately from persisted active identities. Missing/unavailable targets retain their source, attempt time and last successful lookup. A successful identity lookup is not health evidence. Removed targets are paginated behind an explicit toggle. Saved identity inspection and incident history retain original UUID/UID and retirement reasons. Existing inactive records without recorded provenance remain labeled historical with an unavailable retirement reason.

## Reviewed CA/server rotation

Confirm continuity outside the application, update the trusted reader and the policy's expected fingerprint, then use the exact old binding:

```sh
python -m cloudops.entrypoints.cli sync-workloads --previous-cluster-fingerprint <exact-old-fingerprint>
```

Rotation requires the same cluster key, the same configured names and a successful read of every currently bound UID. Changed/missing/unreadable UIDs reject the rotation atomically. Do not combine rotation with a scope change or replacement. A different cluster with different UIDs requires a reviewed migration, not this rotation flag. A successful rotation changes the reader binding only; writer policy/authority is not changed.

## API, migration and verification

- `GET /api/workloads` retains its active-workload UUID contract.
- `GET /api/workloads/scope?removed=false&limit=25&offset=0` exposes configured target state, nullable saved identity and sanitized times/reasons. `removed=true` lists removed targets separately.
- `GET /api/workloads/{workload_id}` reads an exact saved identity, including inactive history. All three routes require a current session; Viewer and Operator can read them. Neither new read contacts Kubernetes or creates authority/jobs.

Migration `0004_workload_scope` adds scope-state/target tables and nullable retirement metadata. It preserves existing identity IDs, baseline JSON/digests and account/session/history data. It does not infer deletion/replacement for old inactive rows. Use the existing quarantined backup recovery procedure before any downgrade; dropping scope binding metadata is not a safe authority rollback.

PostgreSQL-backed tests cover preservation, permission boundaries, concurrency, partial failure and legacy/v2 migration. Unit fixtures use a public test CA certificate without its private key. To reproduce focused live proof after starting the existing local HTTPS/K3s lab:

```sh
uv run --project services/backend python scripts/probe-workload-scope.py --ca tmp/local-https-root.crt
```

The probe is restricted to the fixed `cloudops-e01-https` project. It creates `scope26-a`, `scope26-b` and an unlisted Deployment, uses dedicated temporary reader-denial credentials and named probe application accounts, exercises healthy legacy baseline preservation and actual replacement/history/denial/restart behavior, and writes a sanitized receipt under ignored `tmp`. It removes only its new Deployments/ServiceAccount and leaves a v2 policy for the original `checkout`. The original checkout UID/spec and writer allowlist must remain unchanged. Probe application history/accounts remain available for inspection; no credentials appear in its receipt. This is focused scope evidence, not E02 health/baseline-v1 or complete release acceptance.
