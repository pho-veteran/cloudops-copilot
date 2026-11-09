# Scoped Prometheus metric evidence

[#48](https://github.com/pho-veteran/cloudops-copilot/issues/48) implements the metric producer in the [E04 investigation catalog](https://github.com/pho-veteran/cloudops-copilot/issues/7). It reads six fixed Prometheus query slots for the exact configured workload, stores bounded evidence with the existing retention rules and renders a chart, an accessible table and persisted timeline markers. Viewing or comparing this evidence never starts collection, calls a model or authorizes recovery.

The default base stack remains `COLLECTION_SOURCE_MODE=development_fixture`; no Prometheus endpoint is contacted. The adapter is installed only when the deployment chooses `prometheus` and supplies a trusted read configuration. Kubernetes metric/label bindings are deployment configuration, not user input.

## Read configuration

`PROMETHEUS_READ_CONFIG` names a read-only JSON file:

```json
{
  "schema": "PrometheusReadConfig/v1",
  "endpoint_id": "lab-prometheus",
  "producer_version": "prometheus/v3.5.0",
  "server": "https://prometheus.example:8443/prometheus",
  "ca_data": "<base64 PEM>",
  "token": "<optional bearer token>",
  "step_seconds": 30,
  "binding": {
    "deployment_uid_label": "deployment_uid",
    "namespace_label": "namespace"
  },
  "queries": {
    "request_rate": {"query": "sum by (namespace, deployment_uid) (rate(...))", "unit": "requests/s"},
    "error_ratio": {"query": "sum by (namespace, deployment_uid) (...)", "unit": "ratio"},
    "p95_latency": {"query": "histogram_quantile(...) with namespace/deployment_uid", "unit": "seconds"},
    "memory_working_set": {"query": "sum by (namespace, deployment_uid) (...)", "unit": "bytes"},
    "memory_limit": {"query": "sum by (namespace, deployment_uid) (...)", "unit": "bytes"},
    "restarts": {"query": "sum by (namespace, deployment_uid) (increase(...))", "unit": "count"}
  }
}
```

Validation fails closed at API/worker startup when the schema, endpoint, CA, token shape, step, binding or exact six query IDs are invalid. The server must be one fixed HTTPS URL with an optional path prefix; credentials in the URL, query/fragment, backslashes and percent escapes are rejected. `step_seconds` is 30–300. Each query is a server-owned template that must bind both `{{namespace}}` and `{{workload_uid}}`; no other placeholder is accepted in this slice. Values are escaped before substitution. Every query must **retain** the configured binding labels in its returned series, for example with `sum by (namespace, deployment_uid)`. An aggregation that drops them is rejected as unproven ownership rather than accepted by name alone. No request parameter selects an endpoint, arbitrary PromQL or a credential.

The client uses verified TLS from `ca_data`, no environment proxy, no redirects and a single connection. Each read has an `asyncio.timeout` that covers connection, headers and a slow body, plus a 2 MiB response cap and a 5-second share of the attempt deadline. Provider warnings are recorded as a count, never copied verbatim. Tokens, CA material and response bodies are never logged or stored as evidence.

## Binding and series identity

Every returned series must carry the configured UID label equal to the Run's immutable workload UID and the configured namespace label equal to the Run's namespace. Unmatched series are dropped. If no matching series has finite samples, coverage is `unavailable` with `ownership_unproven`; if some remain, coverage is `partial` with the same reason. A same-name Deployment replacement therefore cannot align old series to the new UID. The configured label proof is stable across Pod churn and does not depend on a current Pod list.

The stored series shape is bounded and allowlisted:

```json
{
  "series": [
    {
      "uid": "<sha256 of the canonical full label set>",
      "name": "cloudops_requests_per_second",
      "namespace": "demo",
      "workload_uid": "<uid>",
      "pod": "checkout-fixture",
      "container": "checkout",
      "unit": "requests/s",
      "samples": [[1730000000, 12.5], [1730000030, 13.1]]
    }
  ]
}
```

`uid` is the semantic series identity; arbitrary label values are represented only through that digest. At most 25 series are retained per query, in canonical `uid` order, and the existing storage cap keeps at most 20,000 samples per Run. Missing values are not zero; non-finite or malformed entries are omitted and counted in `query_metadata.warnings`. `NaN`/`Inf` never become a chart point or a zero sample.

Each metric source stores:

```json
{
  "query_id": "request_rate",
  "query_version": "standard_v1",
  "binding_version": "uid_label_v1",
  "producer_version": "prometheus/v3.5.0",
  "query": "<template>",
  "resolved_query": "<substituted query>",
  "window": {"start": "<UTC>", "end": "<UTC>"},
  "step": 30,
  "unit": "requests/s",
  "warnings": ["2 malformed or non-finite sample entries were omitted."],
  "source_identity": {
    "endpoint_id": "lab-prometheus",
    "cluster_id": "local-k3s",
    "namespace": "demo",
    "workload_uid": "<uid>"
  }
}
```

The existing evidence storage applies whitelisting, credential redaction, deterministic truncation, the 256 KiB per-slot allocation and canonical SHA-256. A Run without a captured window returns `not_captured` without inventing a query window.

## Coverage and UI states

| Situation | Coverage | Reasons |
|---|---|---|
| Finite samples, matching binding, no truncation | `available` | none |
| Some finite samples but gaps/non-finite/truncation | `partial` | `no_samples`, `truncated`, `stale` |
| No finite samples | `unavailable` | `no_samples` |
| No series matches the configured identity | `unavailable` | `ownership_unproven` |
| Provider denied the read | `unavailable` | `forbidden` |
| Total deadline exceeded | `unavailable` | `timeout` |
| Provider/transport/JSON failure | `unavailable` | `source_unavailable` |
| Response exceeded the byte cap | `unavailable` | `truncated` |
| Run window was not captured | `unavailable` | `not_captured` |

The traffic pack requires `request_rate`, `error_ratio` and `p95_latency`; when those three are available it can be `available` even while Kubernetes state/events/logs remain missing. Other packs keep their missing required sources. No pack result is an RCA or a recovery authorization.

The selected evidence panel replaces the raw JSON view for `series` with a bounded SVG chart, a series selector, an accessible sample table, query metadata and the timeline. A missing or empty series prints “No finite samples were returned … Missing data is not zero”; `ownership_unproven` is shown explicitly. Chart and table use browser-local time labels and retain labels/units/gaps in light and dark themes at desktop and 320/390 px. Viewer access remains read-only.

## Timeline

`GET /api/incidents/{incident_id}/runs/{run_id}/timeline` (operation ID `get_run_timeline`) returns at most 500 persisted markers and a 128 KiB response:

- the Run's captured window start/end;
- E02 `WorkloadObservation.observed_at` markers with health/coverage/generation (rollout observation time, not a claimed deployment occurrence);
- E03 incident activity with the recorded creation time and a bounded text excerpt;
- E07 execution creation/finish timestamps with execution/plan IDs and state.

Markers are sorted by time/kind/ID; a missing window returns no markers and an explicit limitation. Alignment is correlation only. The endpoint authorizes the exact incident/Run, reads only PostgreSQL and never calls a provider or mutates state. Both application roles may read it.

## Bounds and concurrency

- Six metric slots, 30-second default step, at most 25 series per slot.
- 2 MiB per query response, 5-second read share, 90-second attempt deadline.
- Existing 20,000 samples and 8 MiB Run envelopes remain authoritative.
- No migration, new table or background worker. The existing checkpoint/publication and dispatch-permit rules apply unchanged.
- Provider reads happen outside database transactions; publication remains inside the fenced finish/checkpoint transaction.

## Local lab

The metrics lab is an optional overlay on the existing disposable E01 lab. It adds a labeled metrics fixture, the Prometheus scrape target and the Caddy `/prometheus/*` TLS route. The fixture is a lab source, **not** an app exporter; its series carry the configured Deployment UID and namespace. Start the TLS proxy first so the CA and read config can be written before the API/worker start:

```sh
uv run --project services/backend python scripts/bootstrap-integration.py
LAB="docker compose -p cloudops-e01-https -f compose.yaml -f compose.https.yaml -f compose.integration.yaml -f compose.metrics.yaml"
$LAB up -d --build --wait --wait-timeout 240 --no-deps https prometheus metrics-fixture
uv run --project services/backend python scripts/bootstrap-metrics.py
$LAB up -d --build --wait --wait-timeout 240 api worker
$LAB exec api python -m cloudops.entrypoints.cli account operator --role operator
$LAB exec api python -m cloudops.entrypoints.cli account viewer --role viewer
```

The fixture mode is selected before starting it:

```sh
METRICS_FIXTURE_MODE=finite|empty|nonfinite|gapped $LAB up -d metrics-fixture
```

`finite` exposes all six sources; `empty` exposes none; `nonfinite` emits `NaN` values; `gapped` intermittently omits series. For `empty`/`nonfinite`/`gapped`, restart `metrics-fixture` and create a new Run so the stored snapshot reflects the selected mode.

Run the read-only receipt after the accounts exist:

```sh
printf '%s\n%s\n' "$OPERATOR_PASSWORD" "$VIEWER_PASSWORD" |
  uv run --project services/backend python scripts/probe-metrics.py \
    --ca tmp/e01-integration/prometheus/ca.crt --password-stdin
```

The probe creates one incident/Run through the real API/worker, checks six metric sources, query metadata, timeline markers, Viewer reads and optional API/worker restart with the same evidence IDs/digests. It writes `tmp/prometheus48-receipt.json`; keep sanitized receipts under `docs/evidence/prometheus48/`. It never prints passwords or tokens. Stop and remove only the task-owned Compose project after recording results; do not remove unrelated volumes.

## Verification and limits

Focused backend cases cover config rejection, TLS/HTTP failure classification, total deadline, binding/replacement, non-finite and empty samples, deterministic truncation, batch/checkpoint parity, storage validation, timeline scoping and permissions. Web cases cover the chart/table, missing data, warnings, series selection and timeline failure. The repository suite, Ruff, generated-contract/drift and web build remain required.

The lab uses an actual pinned Prometheus plus a labeled metrics fixture; it is not a live application exporter, a qualified production telemetry installation or an availability SLO. Denied/timeout/oversized behavior uses a controlled transport, while finite/empty/gapped/non-finite data uses the real Prometheus query path. AI diagnosis, live Kubernetes packs, recovery execution and E10 deployment remain with their owners. This slice does not close E04 or the system release.

## Recorded local acceptance

- [`api.json`](evidence/prometheus48/api.json): real TLS Prometheus read, six `prometheus.*` sources with unit/window/step/query metadata, traffic pack `available`, Run `insufficient_evidence`/`analysis_unavailable`, Viewer read and API/worker restart preserving evidence IDs and SHA-256.
- [`browser.json`](evidence/prometheus48/browser.json) and screenshots: chart, accessible table and timeline markers on the loopback HTTP development origin at 1280/390/320 px, light/dark, Viewer read-only, and no horizontal overflow. Edge was not running, so an isolated managed Chromium was used; no certificate or trust bypass was applied.
- The lab fixture is labeled synthetic telemetry. It is not an application exporter, live RCA or recovery evidence.
