import type { components } from "@/lib/api/generated/schema";

export function WorkloadIdentity({
  workload,
}: {
  workload: components["schemas"]["WorkloadView"];
}) {
  const retirement = {
    deleted: "Deployment deleted",
    replaced: "Replaced by a different Deployment UID",
    removed: "Removed from configured scope",
  };
  const scope = workload.active
    ? "Configured"
    : workload.retirement_reason
      ? retirement[workload.retirement_reason]
      : "Historical identity · retirement reason unavailable";
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
        <dd>{scope}</dd>
        <dt>Source</dt>
        <dd>{workload.source === "live_kubernetes" ? "Live Kubernetes identity" : "Development fixture"}</dd>
        <dt>Live health</dt>
        <dd>Unavailable</dd>
      </dl>
      <p className="muted">
        The saved record ID and Deployment UID identify this workload. A
        replacement UID creates a different record.
      </p>
    </details>
  );
}
