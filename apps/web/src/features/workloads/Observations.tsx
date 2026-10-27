import type { components } from "@/lib/api/generated/schema";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useRelatedIncidents } from "@/features/incidents/queries";
import {
  useRequestObservationRefresh,
  useWorkloadObservation,
  useWorkloadObservationHistory,
} from "./queries";

type ObservationView = components["schemas"]["WorkloadObservationView"];
type ObservationPayload = components["schemas"]["WorkloadObservationPayload"];
type ObservationHistoryItem =
  components["schemas"]["WorkloadObservationHistoryItem"];

const STATUS_LABELS: Record<ObservationPayload["status"], string> = {
  healthy: "Healthy",
  progressing: "Progressing",
  degraded: "Degraded",
  scaled_to_zero: "Scaled to zero",
  unknown: "Unknown",
};

const FRESHNESS_LABELS: Record<ObservationView["freshness"], string> = {
  fresh: "Fresh",
  stale: "Stale",
  unavailable: "Unavailable",
};

type ContainerField = components["schemas"]["ObservedContainerFields"];

function time(value: string) {
  return new Date(value).toLocaleString();
}

function attemptLabel(state: ObservationView["last_attempt_state"]) {
  return state === "succeeded"
    ? "Succeeded"
    : state === "failed"
      ? "Failed"
      : "Not attempted";
}

function resourceSummary(
  resources: ContainerField["resources"],
  kind: "requests" | "limits",
) {
  const group = resources?.[kind];
  if (!group || typeof group !== "object") return "not set";
  const entries = Object.entries(group);
  return entries.length
    ? entries.map(([name, value]) => `${name}=${String(value)}`).join(", ")
    : "not set";
}

function podRestarts(pod: ObservationPayload["pods"][number]) {
  return pod.containers.reduce(
    (total, container) => total + container.restart_count,
    0,
  );
}

function ContainerFields({ container }: { container: ContainerField }) {
  return (
    <div>
      <p className="mono">{container.name ?? "unnamed container"}</p>
      <p>Image: {container.image ?? "not reported"}</p>
      <p>Pull policy: {container.image_pull_policy ?? "not reported"}</p>
      <p>
        Environment names:{" "}
        {container.env?.length ? container.env.join(", ") : "none"}
      </p>
      <p>CPU/memory requests: {resourceSummary(container.resources, "requests")}</p>
      <p>CPU/memory limits: {resourceSummary(container.resources, "limits")}</p>
      <p>
        Ports:{" "}
        {container.ports?.length
          ? container.ports
              .map(
                (port) =>
                  `${port.name ? `${port.name}: ` : ""}${
                    port.container_port ?? "unknown"
                  }`,
              )
              .join(", ")
          : "none"}
      </p>
    </div>
  );
}

function ObservationBody({ view }: { view: ObservationView }) {
  const observation = view.observation!;
  const template = observation.template_fields;
  const historical = Boolean(
    view.last_attempt_state === "failed" &&
    view.last_attempt_at &&
    view.observed_at &&
    new Date(view.last_attempt_at) > new Date(view.observed_at),
  );
  const coverage = observation.coverage.complete
    ? "Coverage complete"
    : `Coverage partial — missing: ${
        observation.coverage.missing_sources.join(", ") || "unknown sources"
      }`;
  const restartDelta =
    observation.restart_delta === null ||
    observation.restart_delta === undefined
      ? "Restart delta unavailable · no comparable previous Pod set"
      : observation.restart_delta > 0
        ? `+${observation.restart_delta} since the previous successful observation`
        : "No new restarts since the previous successful observation";
  return (
    <>
      <div className="foundation-actions">
        <Badge>{STATUS_LABELS[observation.status]}</Badge>
        <Badge variant="outline">{FRESHNESS_LABELS[view.freshness]}</Badge>
        {view.pending_refresh && (
          <Badge variant="outline">Refresh pending</Badge>
        )}
      </div>
      <dl className="foundation-metadata">
        <dt>Last successful observation</dt>
        <dd>
          {view.observed_at ? time(view.observed_at) : "Not recorded"}
          {historical && " · historical"}
        </dd>
        <dt>Last attempt</dt>
        <dd>
          {view.last_attempt_at
            ? `${time(view.last_attempt_at)} · ${attemptLabel(
                view.last_attempt_state,
              )}`
            : "Not attempted"}
          {view.last_attempt_reason ? ` · ${view.last_attempt_reason}` : ""}
        </dd>
        <dt>Coverage</dt>
        <dd>{coverage}</dd>
        <dt>Replicas</dt>
        <dd>
          Ready {observation.ready_replicas}/{observation.desired_replicas} ·{" "}
          {observation.available_replicas} available ·{" "}
          {observation.updated_replicas} updated ·{" "}
          {observation.total_replicas} total
        </dd>
        <dt>Containers</dt>
        <dd>
          {observation.ready_containers}/{observation.total_containers} ready
        </dd>
        <dt>Restarts</dt>
        <dd>
          {observation.restart_total} total · {restartDelta}
        </dd>
        <dt>Generation</dt>
        <dd>
          {observation.generation} · observed{" "}
          {observation.observed_generation ?? "not reported"}
        </dd>
      </dl>
      {observation.reasons.length > 0 && (
        <p className="muted">
          Observed reasons:{" "}
          <span className="mono">{observation.reasons.join(", ")}</span>
        </p>
      )}
      <p className="muted">Application telemetry: unverified.</p>
      {view.freshness === "stale" && (
        <p className="muted">
          The last successful observation is older than 60 seconds.
        </p>
      )}
      {historical && (
        <p className="muted">
          The current values come from the last successful observation. The
          newer attempt did not replace them.
        </p>
      )}
      <details>
        <summary>Observed configuration</summary>
        <p>Service account: {template.service_account_name ?? "default"}</p>
        {(template.containers ?? []).map((container, index) => (
          <ContainerFields
            key={`${container.name ?? "container"}-${index}`}
            container={container}
          />
        ))}
        {(template.init_containers ?? []).length > 0 && (
          <>
            <p>Init containers</p>
            {(template.init_containers ?? []).map((container, index) => (
              <ContainerFields
                key={`${container.name ?? "init"}-${index}`}
                container={container}
              />
            ))}
          </>
        )}
      </details>
      {observation.config_references.length > 0 && (
        <details>
          <summary>Configuration references</summary>
          <ul>
            {observation.config_references.map((reference) => (
              <li
                key={`${reference.kind}:${reference.source}:${reference.name}`}
                className="mono"
              >
                {reference.kind} · {reference.name} · {reference.source}
              </li>
            ))}
          </ul>
        </details>
      )}
      <details>
        <summary>Observed Pods</summary>
        <p>
          {observation.pods.length} Pods ·{" "}
          {observation.pods.filter((pod) => pod.ready).length} ready
        </p>
        <ul>
          {observation.pods.map((pod) => (
            <li key={pod.uid}>
              {pod.name} · {pod.phase ?? "unknown"} ·{" "}
              {pod.ready ? "ready" : "not ready"} · {podRestarts(pod)} restarts
            </li>
          ))}
        </ul>
      </details>
    </>
  );
}

function ObservationHistory({ items }: { items: ObservationHistoryItem[] }) {
  return (
    <details>
      <summary>Recent successful observations</summary>
      <ul>
        {items.map((item) => (
          <li key={item.id}>
            {time(item.observed_at)} · {STATUS_LABELS[item.status]} · restarts{" "}
            {item.payload.restart_total}
          </li>
        ))}
      </ul>
    </details>
  );
}

function RelatedIncidents({ id }: { id: string }) {
  const incidents = useRelatedIncidents(id);
  return (
    <div>
      <h4>Related incidents</h4>
      <p className="muted">Exact Deployment UID matches only.</p>
      {incidents.isPending ? (
        <p role="status">Loading related incidents…</p>
      ) : incidents.isError ? (
        <Failure error={incidents.error} retry={() => void incidents.refetch()} />
      ) : !incidents.data.items.length ? (
        <p>No incidents for this exact workload identity.</p>
      ) : (
        <div className="foundation-stack">
          {incidents.data.items.map((incident) => (
            <a
              className="foundation-panel foundation-incident"
              key={incident.id}
              href={`#/incidents/${incident.id}`}
            >
              <div>
                <strong>{incident.title}</strong>
                <p className="muted">
                  {new Date(incident.created_at).toLocaleString()} ·{" "}
                  {incident.id}
                </p>
              </div>
              <Badge variant="outline">{incident.state}</Badge>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

export function Observations({
  id,
  canMutate,
}: {
  id: string;
  canMutate: boolean;
}) {
  const observation = useWorkloadObservation(id);
  const history = useWorkloadObservationHistory(id);
  const refresh = useRequestObservationRefresh(id);
  return (
    <section className="foundation-stack" aria-label="Observed workload health">
      <h3>Observed health</h3>
      <p className="muted">
        Direct read-only Kubernetes observation · application telemetry
        unverified.
      </p>
      {observation.isPending ? (
        <p role="status">Loading observation…</p>
      ) : observation.isError ? (
        <Failure
          error={observation.error}
          retry={() => void observation.refetch()}
        />
      ) : (
        <>
          {observation.data.source === "development_fixture" ? (
            <p role="status">
              Development fixture · live health unavailable
            </p>
          ) : !observation.data.observation ||
            observation.data.availability === "unavailable" ? (
            <>
              <p role="status">Observation unavailable</p>
              {observation.data.last_attempt_at && (
                <p className="muted">
                  Last attempt {time(observation.data.last_attempt_at)} ·{" "}
                  {attemptLabel(observation.data.last_attempt_state)}
                  {observation.data.last_attempt_reason
                    ? ` · ${observation.data.last_attempt_reason}`
                    : ""}
                </p>
              )}
            </>
          ) : (
            <ObservationBody view={observation.data} />
          )}
          {observation.data.source === "live_kubernetes" && (
            <>
              {!observation.data.workload_active ? (
                <p className="muted">
                  Removed from configured scope · observation refresh is
                  unavailable.
                </p>
              ) : canMutate ? (
                <div className="foundation-actions">
                  <Button
                    variant="outline"
                    disabled={refresh.isPending}
                    onClick={() => refresh.mutate()}
                  >
                    Request observation refresh
                  </Button>
                </div>
              ) : (
                <p className="muted">
                  Viewer access · an Operator must request a refresh.
                </p>
              )}
              {refresh.isError && <Failure error={refresh.error} />}
              {observation.data.pending_refresh && (
                <p role="status">
                  Refresh pending · waiting for the observer.
                </p>
              )}
            </>
          )}
          {history.data && history.data.items.length > 0 && (
            <ObservationHistory items={history.data.items} />
          )}
          <RelatedIncidents id={id} />
        </>
      )}
    </section>
  );
}
