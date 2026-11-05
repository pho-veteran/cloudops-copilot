import { useEffect, useState } from "react";
import { useIncidents } from "./queries";
import {
  buildIncidentHash,
  buildWorkQueueHash,
  parseWorkQueueHash,
  type WorkQueueParams,
} from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function hasFilters(params: WorkQueueParams) {
  return Boolean(
    params.search ||
      params.workload ||
      params.origin ||
      params.state ||
      params.severity ||
      params.alert_signal ||
      (params.view && params.view !== "all") ||
      params.run_state ||
      params.execution_class,
  );
}

export function WorkQueue() {
  const [params, setParams] = useState<WorkQueueParams>(() =>
    parseWorkQueueHash(window.location.hash),
  );
  const [workload, setWorkload] = useState(params.workload ?? "");

  useEffect(() => {
    const sync = () => setParams(parseWorkQueueHash(window.location.hash));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  useEffect(() => {
    const next = buildWorkQueueHash(params);
    if (window.location.hash !== next)
      window.history.replaceState(null, "", next);
  }, [params]);

  useEffect(() => {
    setWorkload(params.workload ?? "");
  }, [params.workload]);

  const query = useIncidents(params.offset ?? 0, {
    search: params.search,
    workload_id: params.workload,
    origin: params.origin,
    state: params.state,
    severity: params.severity,
    alert_signal: params.alert_signal,
    sort_by: params.sort_by,
    sort_order: params.sort_order,
    view: params.view,
    run_state: params.run_state,
    execution_class: params.execution_class,
    anchor_created_at: params.anchor_created_at,
    anchor_id: params.anchor_id,
  });

  useEffect(() => {
    const anchor = query.data?.creation_anchor;
    if (!params.anchor_created_at && anchor && !query.isFetching) {
      setParams((previous) => ({
        ...previous,
        anchor_created_at: anchor.created_at,
        anchor_id: anchor.id,
      }));
    }
  }, [params.anchor_created_at, query.data?.creation_anchor, query.isFetching]);

  const updateFilters = (updates: Partial<WorkQueueParams>) => {
    setParams((previous) => ({ ...previous, ...updates, offset: 0 }));
  };

  const updateOffset = (offset: number) => {
    setParams((previous) => ({ ...previous, offset }));
  };

  const changeWorkload = (value: string) => {
    setWorkload(value);
    if (!value) updateFilters({ workload: undefined });
    else if (uuid.test(value)) updateFilters({ workload: value });
  };

  return (
    <section>
      <div className="eyebrow">PERSISTED APPLICATION DATA</div>
      <h1>Work queue</h1>
      <p className="muted">
        Search, filter, and prioritize saved incidents in your local workspace.
      </p>

      <div className="work-queue-controls">
        <select
          className="foundation-select"
          aria-label="Operational view"
          value={params.view ?? "default"}
          onChange={(event) =>
            updateFilters({
              view:
                event.target.value === "default"
                  ? undefined
                  : (event.target.value as WorkQueueParams["view"]),
            })
          }
        >
          <option value="default">Current work (open and acknowledged)</option>
          <option value="all">All lifecycle states</option>
          <option value="needs_attention">Needs attention</option>
          <option value="awaiting_decision">Awaiting decision</option>
          <option value="verifying">Verifying</option>
          <option value="blocked">Blocked</option>
          <option value="resolved">Resolved</option>
        </select>

        <input
          type="search"
          className="foundation-input"
          aria-label="Search incidents"
          placeholder="Search by title or ID..."
          maxLength={160}
          value={params.search ?? ""}
          onChange={(event) =>
            updateFilters({ search: event.target.value || undefined })
          }
        />

        <select
          className="foundation-select"
          aria-label="Filter by Run state"
          value={params.run_state ?? "all"}
          onChange={(event) =>
            updateFilters({
              run_state:
                event.target.value === "all"
                  ? undefined
                  : (event.target.value as WorkQueueParams["run_state"]),
            })
          }
        >
          <option value="all">All Run states</option>
          <option value="queued">Queued</option>
          <option value="collecting">Collecting</option>
          <option value="analyzing">Analyzing</option>
          <option value="waiting_for_quota">Waiting for quota</option>
          <option value="failed">Failed</option>
          <option value="insufficient_evidence">Insufficient evidence</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
        </select>

        <select
          className="foundation-select"
          aria-label="Filter by execution"
          value={params.execution_class ?? "all"}
          onChange={(event) =>
            updateFilters({
              execution_class:
                event.target.value === "all"
                  ? undefined
                  : (event.target.value as WorkQueueParams["execution_class"]),
            })
          }
        >
          <option value="all">All execution states</option>
          <option value="active">Active</option>
          <option value="terminal_unknown">Outcome unknown</option>
          <option value="unrecognized">Unsupported or ambiguous</option>
          <option value="terminal">Terminal known</option>
        </select>

        <select
          className="foundation-select"
          aria-label="Filter by state"
          value={params.state ?? "all"}
          onChange={(event) =>
            updateFilters({
              state:
                event.target.value === "all"
                  ? undefined
                  : (event.target.value as WorkQueueParams["state"]),
            })
          }
        >
          <option value="all">Use view lifecycle scope</option>
          <option value="open">Open</option>
          <option value="acknowledged">Acknowledged</option>
          <option value="resolved">Resolved</option>
        </select>

        <select
          className="foundation-select"
          aria-label="Filter by alert signal"
          value={params.alert_signal ?? "all"}
          onChange={(event) =>
            updateFilters({
              alert_signal:
                event.target.value === "all"
                  ? undefined
                  : (event.target.value as WorkQueueParams["alert_signal"]),
            })
          }
        >
          <option value="all">All alerts</option>
          <option value="firing">Firing</option>
          <option value="resolved">Resolved</option>
          <option value="none">No alert</option>
        </select>

        <select
          className="foundation-select"
          aria-label="Filter by origin"
          value={params.origin ?? "all"}
          onChange={(event) =>
            updateFilters({
              origin:
                event.target.value === "all"
                  ? undefined
                  : (event.target.value as WorkQueueParams["origin"]),
            })
          }
        >
          <option value="all">All origins</option>
          <option value="operator">Manual</option>
          <option value="alertmanager">Alert</option>
        </select>

        <input
          type="text"
          className="foundation-input"
          aria-label="Filter by severity"
          placeholder="Severity (exact)"
          maxLength={32}
          value={params.severity ?? ""}
          onChange={(event) =>
            updateFilters({ severity: event.target.value || undefined })
          }
        />

        <input
          type="text"
          className="foundation-input"
          aria-label="Filter by workload UUID"
          aria-invalid={Boolean(workload) && !uuid.test(workload)}
          placeholder="Workload UUID"
          spellCheck={false}
          value={workload}
          onChange={(event) => changeWorkload(event.target.value)}
        />

        <select
          className="foundation-select"
          aria-label="Sort incidents"
          value={params.sort_by ?? "last_seen_at"}
          onChange={(event) => {
            const sort_by = event.target
              .value as WorkQueueParams["sort_by"];
            updateFilters({
              sort_by,
              sort_order: sort_by === "severity" ? "asc" : "desc",
            });
          }}
        >
          <option value="created_at">Created time</option>
          <option value="first_seen_at">First seen</option>
          <option value="last_seen_at">Last seen</option>
          <option value="severity">
            Severity ({params.sort_order === "asc" ? "A→Z" : "Z→A"})
          </option>
        </select>

        {hasFilters(params) && (
          <Button
            variant="ghost"
            onClick={() => setParams(parseWorkQueueHash("#/queue"))}
          >
            Clear filters
          </Button>
        )}
      </div>

      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {query.data.items.length === 0 ? (
            <div className="foundation-panel">
              {(params.offset ?? 0) > 0 ? (
                <>
                  <h2>No incidents on this page</h2>
                  <p>Earlier pages may contain incidents. Use Previous or refresh new work.</p>
                </>
              ) : hasFilters(params) ? (
                <>
                  <h2>No matching incidents</h2>
                  <p>Try adjusting your filters or search terms.</p>
                </>
              ) : (
                <>
                  <h2>
                    {params.view === "all"
                      ? "No incidents yet"
                      : "No open or acknowledged incidents"}
                  </h2>
                  <p>View all lifecycle states to include resolved incidents.</p>
                  <a href="#/workloads">View workloads</a>
                </>
              )}
            </div>
          ) : (
            <div className="foundation-stack">
              {query.data.items.map((incident) => (
                <a
                  className={`foundation-panel foundation-incident ${
                    params.selected === incident.id ? "selected" : ""
                  }`}
                  key={incident.id}
                  href={buildIncidentHash(
                    incident.id,
                    "",
                    buildWorkQueueHash({ ...params, selected: incident.id }),
                  )}
                  onClick={() =>
                    window.history.replaceState(
                      null,
                      "",
                      buildWorkQueueHash({
                        ...params,
                        selected: incident.id,
                      }),
                    )
                  }
                >
                  <div className="incident-header">
                    <strong>{incident.title}</strong>
                    <div className="incident-badges">
                      <Badge variant="outline">Incident: {incident.state}</Badge>
                      <Badge variant={incident.alert_signal === "firing" ? "destructive" : "secondary"}>
                        Alert signal: {incident.alert_signal}
                      </Badge>
                      {incident.severity.value && (
                        <Badge variant="secondary">
                          Severity: {incident.severity.value} · {incident.severity.provenance}
                        </Badge>
                      )}
                      {!incident.severity.value && (
                        <Badge variant="secondary">Severity not provided</Badge>
                      )}
                    </div>
                  </div>

                  <div className="incident-meta">
                    <p className="muted">
                      {incident.origin.kind === "operator" ? "Manual" : "Alert"}{" "}
                      · {new Date(incident.created_at).toLocaleString()}
                      {incident.first_seen_at
                        ? ` · First seen ${new Date(
                            incident.first_seen_at,
                          ).toLocaleString()}`
                        : " · First seen unknown"}
                      {incident.last_seen_at
                        ? ` · Last seen ${new Date(
                            incident.last_seen_at,
                          ).toLocaleString()}`
                        : " · Last seen unknown"}
                    </p>
                    <p className="muted text-xs">
                      {incident.origin.kind === "alertmanager"
                        ? "Opened from Alertmanager"
                        : `Opened by ${incident.created_by_name ?? "unknown requester"}`}
                      {incident.acknowledged_at
                        ? ` · Acknowledged by ${incident.acknowledged_by_name ?? "unknown actor"} at ${new Date(incident.acknowledged_at).toLocaleString()}`
                        : incident.state === "acknowledged"
                          ? " · Acknowledgment actor/time unknown"
                          : ""}
                    </p>
                    {incident.latest_run && (
                      <p className="muted text-xs">
                        Run: {incident.latest_run.state} · {incident.latest_run.id}
                        {incident.latest_run.queue_reason === "capacity"
                          ? " · Deferred: dispatch capacity"
                          : ""}
                        {incident.latest_run.requested_by_name
                          ? ` · Requested by ${incident.latest_run.requested_by_name}`
                          : incident.latest_run.origin_kind === "alertmanager"
                            ? " · Requested automatically by Alertmanager"
                            : " · Requester unknown"}
                      </p>
                    )}
                    {!incident.latest_run && <p className="muted text-xs">Run: none recorded</p>}
                    {(incident.run_blockers ?? []).length > 0 && (
                      <p className="text-xs" role="status">
                        Run blockers in history: {incident.run_blockers.join(", ").replaceAll("_", " ")}
                      </p>
                    )}
                    {(incident.relevant_executions ?? []).map((execution) => (
                      <p className="text-xs" role="status" key={execution.id}>
                        Execution: {execution.state} · {execution.classification.replaceAll("_", " ")}
                        {execution.holds_lock ? " · target locked" : " · target lock released"}
                      </p>
                    ))}
                    {(incident.relevant_executions ?? []).length === 0 && (
                      <p className="muted text-xs">Active or unknown execution: none recorded</p>
                    )}
                    <p className="muted text-xs">{incident.id}</p>
                  </div>
                </a>
              ))}
            </div>
          )}

          <div className="foundation-actions">
            {(query.data.items.length > 0 || (params.offset ?? 0) > 0) && (
              <>
                <Button
                  variant="outline"
                  disabled={(params.offset ?? 0) === 0}
                  onClick={() =>
                    updateOffset(Math.max(0, (params.offset ?? 0) - 25))
                  }
                >
                  Previous
                </Button>
                {query.data.items.length > 0 && (
                  <span className="text-sm text-muted-foreground">
                    Showing {(params.offset ?? 0) + 1}–
                    {(params.offset ?? 0) + query.data.items.length}
                  </span>
                )}
                <Button
                  variant="outline"
                  disabled={query.data.next_offset === null}
                  onClick={() => updateOffset(query.data.next_offset!)}
                >
                  Next
                </Button>
              </>
            )}
            <Button
              variant="ghost"
              disabled={query.isFetching}
              onClick={() =>
                setParams((previous) => ({
                  ...previous,
                  offset: 0,
                  anchor_created_at: undefined,
                  anchor_id: undefined,
                }))
              }
            >
              {query.isFetching ? "Refreshing..." : "Refresh new work"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
