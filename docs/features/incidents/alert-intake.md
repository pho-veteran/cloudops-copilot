# Alertmanager intake

[#36](https://github.com/pho-veteran/cloudops-copilot/issues/36) turns authenticated Alertmanager webhook v4 notifications into durable work: one incident and one initial investigation per firing episode, bound to a configured Deployment UID. Every authenticated per-alert outcome is visible under **Connections → Alert intake**. Intake never calls a model; collection stays the labeled synthetic fixture.

## Configure the sender credential

Intake is disabled until the API process has `ALERT_INTAKE_CONFIG` naming a read-only JSON file:

```json
{
  "schema_version": 1,
  "source_id": "lab-alertmanager",
  "cluster": "local-k3s",
  "token_sha256": "<64 lowercase hex characters>"
}
```

- `source_id` (`^[a-z0-9][a-z0-9-]{0,63}$`) becomes the trusted origin `Alertmanager · <source_id>`. Payload-level or per-alert `source_id`/`origin` claims are refused as invalid envelopes before writes or mapping reads. Other unknown sender fields remain forward-compatible.
- `cluster` must equal the cluster key of the pinned v2 [workload scope](../workloads/scope.md). Until it does, every new firing alert is retryable with reason `scope_unpinned`.
- `token_sha256` is the SHA-256 of a random sender token of at least 32 bytes. The API stores only this digest. Alertmanager reads the token from `credentials_file`. Generate both with:

```sh
python -c "import hashlib,secrets; t=secrets.token_urlsafe(32); print(t); print(hashlib.sha256(t.encode()).hexdigest())"
```

An invalid file stops the API at startup. Rotate the token by updating both files and restarting the API.

The credential is machine authentication only. Browser cookies and sessions never grant intake, and the token is not accepted by any Operator or Viewer endpoint.

## Sender configuration

```yaml
route:
  receiver: cloudops
  group_by: [alertname, namespace]
receivers:
  - name: cloudops
    webhook_configs:
      - url: https://<cloudops-origin>/api/intake/alertmanager
        send_resolved: true
        max_alerts: 100
        http_config:
          authorization:
            type: Bearer
            credentials_file: /run/cloudops-intake/token
```

The response contract follows the [Alertmanager webhook sender](https://github.com/prometheus/alertmanager/blob/main/notify/webhook/webhook.go): a 2xx response is final and a 5xx response is retried.

| Response | Meaning | Recorded |
|---|---|---|
| `200` with `counts` | Every alert in the notification settled | Per-alert receipts |
| `401 intake_unauthorized` | Missing, wrong or repeated `Authorization`, or a cookie-only request | Nothing; standard access log only |
| `503 intake_unavailable` | Intake is not configured | Nothing |
| `413 intake_too_large`, `415 intake_media_type`, `422 intake_invalid` | Body over 1 MiB, not JSON, or not a valid v4 envelope with 1–100 alerts | Nothing |
| `503 intake_retry` | At least one alert hit a transient condition | Receipts for every alert that settled; retryable receipts where the database allowed them |

Authentication happens before the body is read, and the body read stops at 1 MiB. Unauthenticated or malformed requests make no database write, cluster read or model call.

## Supported identity labels

Labels select candidates; ownership is always confirmed against the live cluster through the pinned scope reader. That reader uses fixed API paths, does not follow redirects, and enforces a per-notification budget of 20 GET requests, an absolute 10-second network deadline and 1 MiB per response. In-flight network reads are cancelled at the deadline and return `budget_exhausted`, not a late identity.

| Labels | Resolution |
|---|---|
| `namespace` + `deployment` | `GET` the Deployment; its UID must equal the configured active identity |
| `namespace` + `pod` | `GET` the Pod → its single controlling ReplicaSet → that ReplicaSet's controlling Deployment UID |
| Both | The Pod chain must name the same Deployment |
| `cluster` (optional) | Must equal the configured cluster |
| `alertname`, `severity` | Display and metadata only |

Severity is stored raw with provenance `alert_label` when it matches `^[A-Za-z0-9_.-]{1,32}$`; otherwise it stays `Not provided`. Annotations are size-checked and then discarded. They never appear in persistence, responses or receipts.

## Episodes and outcomes

The deduplication key is `(source_id, fingerprint, startsAt)`. `startsAt` is normalized to UTC with all nine fractional digits, so equivalent spellings match and nanosecond differences stay distinct. The bound Workload UID never changes once recorded.

| Situation | Firing alert | Resolved alert |
|---|---|---|
| New episode, identity verified | `accepted`, or `deferred` while dispatch capacity is full. Creates one incident and Run | `ignored` (`resolved_before_admission`) terminal marker; no incident |
| New episode, permanent identity problem | `rejected` terminal marker with reason | — |
| New episode, transient problem | `retryable`, no marker; Alertmanager retries | — |
| Accepted episode | `duplicate`; updates last received time | `resolved_signal`; sets the incident alert signal only |
| Rejected or ignored episode | `duplicate` with the original reason; never creates work, even after a scope change | `duplicate` |

- A resolved alert never resolves, reopens or cancels anything. Lifecycle stays with Operators.
- A new firing `startsAt` is a new episode and creates a new incident. There is no merging by title or by time proximity.
- An accepted episode keeps its incident and Run identities through cancel, resolve, reopen, redelivery and restart. It never consumes a second automatic admission.
- Each alert settles in its own short transaction, so a permanent rejection never discards the valid alerts in the same batch.
- Capacity follows [bounded admission](admission.md#capacity). When dispatch is full, the Run waits as `queued/capacity` and the worker promotes it automatically. When the deferred queue is also full, the alert is retryable and nothing is saved.

| Reason | Kind |
|---|---|
| `identity_missing`, `identity_invalid`, `identity_ambiguous`, `identity_changed`, `unsupported_identity`, `out_of_scope` | Permanent |
| `resolved_before_admission` | Ignored |
| `scope_unpinned`, `scope_changed`, `cluster_unavailable`, `budget_exhausted`, `capacity_exhausted`, `admission_conflict`, `database_unavailable` | Retryable |

`identity_changed` is permanent. An alert carries no UID, so a Deployment label whose live UID differs from the configured identity cannot be safely attributed to either the old or the replacement identity.

## Lock order

Intake takes the admission lock, then the episode row, then the shared scope-state lock, then the Workload, and only then lets admission create the Incident, Run and Job. Scope reconciliation takes the scope-state lock before workload rows and does not take the admission lock. A repeat takes the admission lock, the episode, then the Incident. Resolve takes the Incident first but never waits for the admission lock, so these orders cannot deadlock. All network reads happen before the transaction.

The reader captures the scope fingerprint and published sequence with each target's observation epoch. Admission rechecks these under the scope and workload locks. A publication, rotation, retirement or epoch change during the read yields retryable `scope_changed` without consuming the episode; a verified same-name replacement remains a permanent identity rejection. Known episodes are still looked up before remapping or capacity enforcement.

## Receipts, retention and redaction

`GET /api/alert-receipts` (Viewer or Operator) returns newest-first, keyset-paginated receipts plus whether intake is configured. A receipt stores only safe fields: time, source, alert status, fingerprint, canonical start, `alertname`, namespace, the target label kind and name, outcome, reason code, and incident/Run/workload links. The newest 5000 receipts are kept and older ones are pruned after each delivery. Episodes are never pruned, because they are the deduplication identity.

## Local lab

The overlay `compose.alertmanager.yaml` runs `prom/alertmanager:v0.34.2`, pinned by digest, in the HTTPS proxy's network namespace, so `https://localhost:8443` and its TLS name work unchanged. The bootstrap writes the sender token to `tmp/e01-integration/intake-sender/` and only its digest to `tmp/e01-integration/intake-api/`.

```sh
export CLOUDOPS_LAB_PROJECT=cloudops-intake36   # optional: a separate disposable project
uv run --project services/backend python scripts/bootstrap-integration.py
LAB="docker compose -p ${CLOUDOPS_LAB_PROJECT:-cloudops-e01-https} -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f compose.alertmanager.yaml"
$LAB up -d --build --wait api worker https
$LAB cp https:/data/caddy/pki/authorities/local/root.crt tmp/e01-integration/intake-sender/proxy-ca.crt
chmod 0644 tmp/e01-integration/intake-sender/proxy-ca.crt
cp tmp/e01-integration/intake-sender/proxy-ca.crt tmp/local-https-root.crt
$LAB up -d --wait alertmanager
uv run --project services/backend python scripts/probe-alert-intake.py --ca tmp/local-https-root.crt
```

The probe pins a v2 scope for `demo/checkout`, signs in a disposable Operator and injects alerts into the real Alertmanager with `amtool`. It then checks, through the receipts API:
- a wrong credential gets 401 and leaves no receipt;
- a direct Deployment alert creates a UID-bound incident;
- a repeat is a duplicate;
- a Pod owner-chain alert maps to the same UID;
- an out-of-scope alert is rejected;
- a resolved alert only sets the signal;
- a new firing start creates a new incident;
- receipts contain no token or annotation.

Remove the disposable project with `$LAB down -v` when finished.

The recorded receipt is [`docs/evidence/intake36-sender.json`](../../evidence/intake36-sender.json). It covers the checks above and an API/worker restart after which Alertmanager repeats remained duplicates, with the same episodes and incidents.

The [browser receipt](../../evidence/intake36-browser.json) records actual Connections journeys at 320px and 390px: Viewer read-only access and a denied write with unchanged history, keyboard refresh/open-incident navigation, 25-to-50-row pagination, a configured empty database, loading, and retry after initial/refresh failures. Existing rows remain visible after a failed refresh; no horizontal overflow was observed. [Capacity outcomes](../../evidence/intake36-capacity-390.jpg), [empty state](../../evidence/intake36-empty-320.jpg), [initial failure](../../evidence/intake36-initial-error-390.jpg), and [signal-only metadata](../../evidence/intake36-signal-only-320.jpg) capture the corresponding surfaces.

These browser journeys use a separate HTTP development origin over the real disposable PostgreSQL records; no certificate warning was bypassed or OS/browser trust changed. Loading and transport errors were deliberately induced in the browser, not claimed as a live cluster outage. The sender receipt separately validates HTTPS with the lab CA. Its labeled capacity fixture used authenticated intake with live UID mapping and a stopped worker to reach the dispatch bound, then resumed the normal worker and observed promotion of the same Run.

## E03 bounded integration probe

`scripts/probe-e03-integration.py` has a recorded run in the
[E03 acceptance ledger](../../acceptance/e03-incident-integration.md), including source binding,
failures, producer limits and retained-volume cleanup. Run it only against a
**disposable, isolated** `cloudops-*` Compose project,
after the local lab above has been bootstrapped, the v2 `demo/checkout` scope
published with `sync-workloads`, and the API, HTTPS proxy, PostgreSQL, K3s, exactly
one worker and pinned Alertmanager are running. The lab CA must validate the
`localhost` hostname. The API and worker must have the same effective J, D and
collector delay, with J and D each between 2 and 100; the probe reads their
effective settings and checks the recreated worker's image and settings. The
project's active Job and deferred queues must be empty, and no other process or
user may submit work while it runs. The script does not use host kubeconfig.
The reserved `cloudops-local` and `cloudops-e01-https` projects are refused.

For example, export these values **before starting the lab services** and retain
the same explicit project, password and settings when invoking the probe. The
default J is 100; this example sets D to 100 and the collector delay to 2 seconds:

```sh
export CLOUDOPS_LAB_PROJECT=cloudops-e03-acceptance
export DEFERRED_RUN_LIMIT=100
export COLLECTOR_DELAY_SECONDS=2
uv run --project services/backend python scripts/probe-e03-integration.py \
  --ca tmp/local-https-root.crt --output tmp/e03-integration-receipt.json
```

The repeat path uses the configured Alertmanager. For concurrent real delivery,
the probe starts two additional pinned Alertmanager containers sharing only the
project proxy network, with separate listen ports and storage, clustering disabled
and repeats delayed. Before injection, it records the identical alert labels and
start time in the partial receipt. It observes both webhook-success metrics and
separate intake deliveries, and removes only these two probe-created containers
and their temporary configuration. An absent
webhook-failure counter series remains `null` in the receipt (the counter vector
has not been incremented), rather than being reported as an observed zero. Direct
concurrent and mixed-batch v4 deliveries are labeled separately. After a saturated
mixed batch returns 503, the probe retries the entire unchanged batch after freeing
capacity, and sends it again to check deduplication.
The probe stops and recreates its own worker without starting it, creates
a disposable Operator and durable incidents, Runs, Jobs, episodes and receipts,
fills the configured capacities through actual APIs, and cancels selected queued
Runs through the Operator API. It records actual image metadata, runner commit,
script SHA-256 (including uncommitted edits), Deployment UID, identifiers and observed
results in a sanitized receipt, including
partial results on failure or interruption. It attempts to restart a stopped worker
unless its recreated image or environment could not be verified; inspect
`worker_restore` and manually start that project's worker if needed. Do not reuse
the project for production or unrelated work.
After retaining the receipt and inspecting the project, remove only the selected
disposable project (including its volumes):

```sh
docker compose -p "$CLOUDOPS_LAB_PROJECT" -f compose.yaml -f compose.https.yaml \
  -f compose.integration.yaml -f compose.alertmanager.yaml down -v
```

To include UID replacement, explicitly add `--replace-owned-checkout` to the
invocation. This **deletes and recreates** the project's `demo/checkout`
Deployment, replacing its UID and Pods; it does not replace any other target.
The probe requires that the live Deployment match the bootstrap writer's pinned
UID, the expected demo container and the v2 scope, and uses UID/resource-version
delete preconditions. It recreates the spec without server identity, status,
owner references or authority annotations, synchronizes scope, and checks the
old episode/Run binding and a new episode's replacement UID. The old writer
allowlist remains pinned to the old UID; replacement does not authorize a new
writer. Before deletion, the probe requires at least one old-UID unknown execution
that still holds its lock. All old-UID unknown-execution rows and locks are compared,
not released. Seed the real retained execution separately; the probe does not create
one. A failure after deletion can leave checkout absent or scope unsynchronized; inspect the receipt
and repair only this disposable lab before using it again. Without the flag the
replacement scenario is explicitly unexercised.

Lifecycle/claim/callback races and full browser acceptance require separate
evidence. Worker recreation occurs while reservations are full and stopped;
the probe frees exactly one Job slot, then starts the replacement worker.
All deferred Runs promote automatically on ordinary worker cycles; the probe
never invokes promotion itself. With at least two deferred Runs and only one
initial slot, later promotion requires capacity freed by worker progress.
This is not a transaction-level fault injection. Oldest-first is checked only
for these deferred Runs without competing admissions; continuous-admission
fairness is not guaranteed. Collection remains synthetic and direct HTTP
fixtures are not live AI or remediation.

## Limits

- One configured source per deployment. There is no rate limiting or mTLS.
- The lab receipt injects alerts through Alertmanager's API; Prometheus alert rules are not part of it.
- Collection is synthetic. The [integrated E03 ledger](../../acceptance/e03-incident-integration.md) maps [#39](https://github.com/pho-veteran/cloudops-copilot/issues/39) and every parent criterion to actual sender/browser/persistence evidence without claiming future RCA or recovery engines.
