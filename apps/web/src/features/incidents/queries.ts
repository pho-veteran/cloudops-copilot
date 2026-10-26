import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const incidentKeys = {
  all: ["incidents"] as const,
  detail: (id: string) => ["incidents", id] as const,
  workload: (id: string) => ["incidents", "workload", id] as const,
};

export function useIncidents(offset = 0) {
  return useQuery({
    queryKey: [...incidentKeys.all, "page", offset],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents", {
          params: { query: { limit: 25, offset } },
        }),
      ),
    refetchInterval: 5000,
  });
}

export function useRelatedIncidents(workloadId: string) {
  return useQuery({
    queryKey: incidentKeys.workload(workloadId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents", {
          params: {
            query: { workload_uuid: workloadId, limit: 25, offset: 0 },
          },
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
