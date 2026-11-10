# Scoped Kubernetes investigation evidence

The optional `live_kubernetes` provider collects fresh Run-bound state, related events, current/previous logs and explicitly enabled ConfigMap metadata. It uses the existing `standard_v1` slots, worker/checkpoint flow, evidence retention and exact historical source routes. Collection finishes as `insufficient_evidence` / `analysis_unavailable`; it does not produce RCA or authorize recovery.

## Enable the provider

Supply a deployment-owned `KUBE_READ_CONFIG` JSON file containing `server` (one fixed HTTPS origin), `ca_data` (base64 PEM CA) and `token` (a reader service-account token). Configure the allowed cluster/namespace/Deployment identities through the existing E02 workload scope. The provider uses only this reader file, verified TLS, GET requests, no environment proxy and no redirects. It never uses an executor client or asks the user question to choose a path, selector or destination.

Set `COLLECTION_SOURCE_MODE=live_kubernetes` on API and worker. `live_kubernetes,prometheus` composes both installed live providers when their read configurations exist. The synthetic fixture cannot be combined with live providers. The default Compose stack remains `development_fixture`; unconfigured Prometheus slots stay explicit missing traffic coverage in Kubernetes-only mode.

The existing [E01 lab](e01-local-lab.md) prepares the mounted reader configuration. Its `config/lab/workload.yaml` reader Role includes `get pods/log`. Apply `config/lab/kubernetes-incident-families.yaml` for CrashLoop, OOM, Pending and ImagePull examples, and `config/lab/kubernetes-log-cap.yaml` for a bounded noisy workload. Add their names/UIDs to the trusted workload configuration and synchronize it using the normal CLI before creating incidents. Keep these fixtures in an owned disposable cluster; do not apply them to shared or production targets.

The reader needs namespace-scoped `get/list` for Deployments, ReplicaSets, Pods and events, plus `get pods/log`. It needs no Secret, mutation or exec permissions. The `pods/log get` grant can also authorize follow/streaming; `follow=false` is enforced by the client, not RBAC.

## Scope, content and bounds

- Verify the Deployment UID before and after each source. Require a unique controlling `apps/v1` Deployment → ReplicaSet → Pod UID chain in the configured namespace. Noncontroller, ambiguous and foreign references cannot establish membership.
- Bracket each log read with the exact Pod name/UID/controller proof. A same-name Pod or Deployment replacement returns `identity_changed`, with no replacement contents published under the old UID.
- Store safe desired-template image/resource fields, revision identifiers and regular/init-container state. Raw environment values, arbitrary labels/annotations, ConfigMap data and Secret contents are excluded.
- Logs request `timestamps=true`, `sinceTime`, `follow=false`, at most 200 tail lines and 32 KiB. Current and previous identity, container kind and redaction/truncation remain visible. Retained timestamps determine whether output extends after the captured end; collection time alone does not imply late log output.
- One cancellable wall-clock deadline covers the whole source, all its reads/body streaming and final identity checks. The shared 5-second source share, 90-second attempt, response-byte, 10-Pod/20-container, 100-event and per-source storage limits remain authoritative. Pagination and producer cutoffs publish `partial/truncated` instead of silently claiming complete coverage. Selection uses stable UID/container order.

A proven never-started/no-restart container can make its corresponding log requirement `not_applicable`. A restarted container whose previous logs are missing remains a gap; mixed successful/missing applicable reads are partial. Truncated required logs keep crash/OOM packs partial. Successful empty events describe the matching retained response, not historical absence. A legacy Run without a captured window produces `not_captured` without making live requests.

## Optional ConfigMap metadata

ConfigMap metadata is disabled by default. To enable it, explicitly grant the reader `get` for the allowed referenced ConfigMap names and set `KUBE_CONFIGMAP_ENABLED=1`. Compose forwards this flag to API/worker. The provider follows only declared workload-template references, requests `PartialObjectMetadata` and rejects unsupported/full-object responses. It never falls back to reading `data`/`binaryData`.

Kubernetes RBAC cannot enforce metadata-only negotiation. A ConfigMap `get` grant authorizes the complete object; metadata-only behavior is enforced by this client. No Secret grant or read is added.

## Inspect a stored Run

Sign in, create an incident for an allowed live workload and select a Kubernetes source in **Evidence**. Metadata identifies the original UID, captured window, time relation, coverage, digest and retention. The bounded JSON preserves state/events/log context and original log language after redaction. A capped log states that only a bounded tail is retained. Source selection keeps exact Run/evidence IDs across keyboard activation, reload and back. Viewers may inspect/export the same stored records and cannot admit mutations.

The [acceptance receipt](evidence/kubernetes47/acceptance.json) separates actual K3s/API/worker and Edge observations from controlled contract fixtures. These checks establish this collection slice, not all-pack E04 completion, a model integration, recovery execution or production telemetry qualification.
