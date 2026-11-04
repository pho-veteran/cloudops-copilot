import { queryOptions, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { operations } from "@/lib/api/generated/schema";

export const incidentKeys = {
  all: ["incidents"] as const,
  detail: (id: string) => ["incidents", id] as const,
};

export interface IncidentFilters {
  search?: string;
  workload_id?: string;
  origin?: "operator" | "alertmanager";
  state?: "open" | "acknowledged" | "resolved";
  severity?: string;
  alert_signal?: "none" | "firing" | "resolved";
  sort_by?: "created_at" | "first_seen_at" | "last_seen_at" | "severity";
  sort_order?: "asc" | "desc";
  view?: "all" | "needs_attention" | "awaiting_decision" | "verifying" | "blocked" | "resolved";
  run_state?: "queued" | "collecting" | "analyzing" | "waiting_for_quota" | "insufficient_evidence" | "completed" | "failed" | "cancelled";
  execution_class?: "active" | "terminal_unknown" | "terminal" | "unrecognized";
  anchor_created_at?: string;
  anchor_id?: string;
}

type IncidentQuery = NonNullable<
  operations["list_incidents"]["parameters"]["query"]
>;

function incidentQuery(
  offset: number,
  filters: IncidentFilters,
): IncidentQuery {
  const query: IncidentQuery = { limit: 25, offset };
  if (filters.search) query.search = filters.search;
  if (filters.workload_id) query.workload_id = filters.workload_id;
  if (filters.origin) query.origin = filters.origin;
  if (filters.state) query.state = filters.state;
  if (filters.severity) query.severity = filters.severity;
  if (filters.alert_signal) query.alert_signal = filters.alert_signal;
  if (filters.sort_by) query.sort_by = filters.sort_by;
  if (filters.sort_order) query.sort_order = filters.sort_order;
  if (filters.view) query.view = filters.view;
  if (filters.run_state) query.run_state = filters.run_state;
  if (filters.execution_class) query.execution_class = filters.execution_class;
  if (filters.anchor_created_at) query.anchor_created_at = filters.anchor_created_at;
  if (filters.anchor_id) query.anchor_id = filters.anchor_id;
  return query;
}

export function incidentQueryOptions(
  offset = 0,
  filters: IncidentFilters = {},
  client = api,
) {
  return queryOptions({
    queryKey: [...incidentKeys.all, "page", offset, filters],
    queryFn: async ({ signal }) =>
      unwrap(
        await client.GET("/api/incidents", {
          signal,
          params: { query: incidentQuery(offset, filters) },
        }),
      ),
    refetchInterval: 5000,
  });
}

export function useIncidents(offset = 0, filters: IncidentFilters = {}) {
  return useQuery(incidentQueryOptions(offset, filters));
}
export function useIncident(id: string) {
  return useQuery({
    queryKey: incidentKeys.detail(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}", {
          params: { path: { incident_id: id } },
        }),
      ),
  });
}
