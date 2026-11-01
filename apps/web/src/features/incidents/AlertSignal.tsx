import type { components } from "@/lib/api/generated/schema";

type Incident = components["schemas"]["IncidentView"];

/** Read-only alert metadata recorded by intake; it never changes the incident lifecycle. */
export function AlertSignal({ incident }: { incident: Incident }) {
  if (incident.origin.kind !== "alertmanager") return null;
  const time = (value: string | null) =>
    value ? new Date(value).toLocaleString() : "Not captured";
  return (
    <dl className="foundation-metadata" aria-label="Alert signal">
      <dt>Alert signal</dt>
      <dd>
        {incident.alert_signal === "resolved"
          ? "Resolved"
          : incident.alert_signal === "firing"
            ? "Firing"
            : "Not captured"}{" "}
        <span className="muted">· Signal only; the incident lifecycle is unchanged.</span>
      </dd>
      <dt>Severity</dt>
      <dd>
        {incident.severity.value
          ? `${incident.severity.value} (alert label)`
          : "Not provided"}
      </dd>
      <dt>First seen (source)</dt>
      <dd>{time(incident.first_seen_at)}</dd>
      <dt>Last received</dt>
      <dd>{time(incident.last_seen_at)}</dd>
    </dl>
  );
}
