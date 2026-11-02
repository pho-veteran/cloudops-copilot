import { useEffect, useState } from "react";
import { useIncidents } from "./queries";
import {
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
      params.alert_signal,
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
  });

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
          <option value="all">All states</option>
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
          value={params.sort_by ?? "created_at"}
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
          <Button variant="ghost" onClick={() => setParams({})}>
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
              {hasFilters(params) ? (
                <>
                  <h2>No matching incidents</h2>
                  <p>Try adjusting your filters or search terms.</p>
                </>
              ) : (
                <>
                  <h2>No incidents yet</h2>
                  <p>Select a workload and start an investigation.</p>
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
                  href={`#/incidents/${incident.id}`}
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
                      <Badge variant="outline">{incident.state}</Badge>
                      {incident.alert_signal !== "none" && (
                        <Badge
                          variant={
                            incident.alert_signal === "firing"
                              ? "destructive"
                              : "secondary"
                          }
                        >
                          {incident.alert_signal}
                        </Badge>
                      )}
                      {incident.severity.value && (
                        <Badge variant="secondary">
                          {incident.severity.value}
                        </Badge>
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
                        : ""}
                    </p>
                    <p className="muted text-xs">{incident.id}</p>
                  </div>
                </a>
              ))}
            </div>
          )}

          {query.data.items.length > 0 && (
            <div className="foundation-actions">
              <Button
                variant="outline"
                disabled={(params.offset ?? 0) === 0}
                onClick={() =>
                  updateOffset(Math.max(0, (params.offset ?? 0) - 25))
                }
              >
                Previous
              </Button>
              <span className="text-sm text-muted">
                Showing {(params.offset ?? 0) + 1}–
                {(params.offset ?? 0) + query.data.items.length}
              </span>
              <Button
                variant="outline"
                disabled={query.data.next_offset === null}
                onClick={() => updateOffset(query.data.next_offset!)}
              >
                Next
              </Button>
              <Button
                variant="ghost"
                disabled={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {query.isFetching ? "Refreshing..." : "Refresh"}
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
