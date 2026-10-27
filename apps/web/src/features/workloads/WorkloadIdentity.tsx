import type { components } from "@/lib/api/generated/schema";
import { useAgedObservation } from "./WorkloadHealth";

export function WorkloadIdentity({
  workload,
}: {
  workload: components["schemas"]["WorkloadView"];
}) {
  const aged = useAgedObservation(workload.observation);
  return (
    <details>
      <summary>Inspect workload identity</summary>
      <dl className="foundation-metadata mt-3">
        <dt>Record ID</dt>
        <dd className="mono">{workload.id}</dd>
        <dt>Cluster</dt>
        <dd>{workload.cluster}</dd>
        <dt>Namespace</dt>
        <dd>{workload.namespace}</dd>
        <dt>Deployment</dt>
        <dd>{workload.name}</dd>
        <dt>Deployment UID</dt>
        <dd className="mono">{workload.uid}</dd>
        <dt>Scope</dt>
        <dd>{workload.active ? "Configured" : "Removed from configured scope"}</dd>
        <dt>Source</dt>
        <dd>{workload.source === "live_kubernetes" ? "Live Kubernetes" : "Development fixture"}</dd>
        <dt>Live health</dt>
        <dd>{aged.health.replaceAll("_", " ")}</dd>
      </dl>
      <p className="muted">
        The saved record ID and Deployment UID identify this workload. A
        replacement UID creates a different record.
      </p>
    </details>
  );
}
