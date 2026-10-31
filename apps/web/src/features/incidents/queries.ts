import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const incidentKeys = {
  all: ["incidents"] as const,
  detail: (id: string) => ["incidents", id] as const,
};
export function useIncidents(offset = 0, workloadId?: string) {
  return useQuery({
    queryKey: [...incidentKeys.all, "page", offset, workloadId],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/incidents", {
          signal,
          params: { query: { limit: 25, offset, workload_id: workloadId } },
        }),
      ),
    refetchInterval: 5000,
  });
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
