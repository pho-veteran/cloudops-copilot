import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const runKeys = { all: (id: string) => ["runs", id] as const };
export function useRuns(incidentId: string, offset = 0) {
  return useQuery({
    queryKey: [...runKeys.all(incidentId), offset],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs", {
          params: {
            path: { incident_id: incidentId },
            query: { limit: 25, offset },
          },
        }),
      ),
    refetchInterval: 2000,
  });
}
export function useRun(incidentId: string, runId: string) {
  return useQuery({
    queryKey: [...runKeys.all(incidentId), "detail", runId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs/{run_id}", {
          params: { path: { incident_id: incidentId, run_id: runId } },
        }),
      ),
    enabled: Boolean(runId),
    refetchInterval: 2000,
  });
}
