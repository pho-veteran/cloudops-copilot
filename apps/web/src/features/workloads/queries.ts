import {
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const workloadKeys = {
  all: ["workloads"] as const,
  observation: (id: string) => ["workloads", id, "observation"] as const,
  observations: (id: string) => ["workloads", id, "observations"] as const,
};

export function workloadsQueryOptions(offset = 0, client = api) {
  return queryOptions({
    queryKey: [...workloadKeys.all, offset],
    queryFn: async () =>
      unwrap(
        await client.GET("/api/workloads", {
          params: { query: { limit: 25, offset } },
        }),
      ),
  });
}

export function useWorkloads(offset = 0) {
  return useQuery(workloadsQueryOptions(offset));
}

export function observationQueryOptions(id: string, client = api) {
  return queryOptions({
    queryKey: workloadKeys.observation(id),
    // The 60-second stale policy is only visible if the projection is re-read.
    refetchInterval: 20_000,
    queryFn: async () =>
      unwrap(
        await client.GET("/api/workloads/{workload_id}/observation", {
          params: { path: { workload_id: id } },
        }),
      ),
  });
}

export function useWorkloadObservation(id: string) {
  return useQuery(observationQueryOptions(id));
}

export function observationHistoryQueryOptions(
  id: string,
  limit = 5,
  client = api,
) {
  return queryOptions({
    queryKey: workloadKeys.observations(id),
    refetchInterval: 30_000,
    queryFn: async () =>
      unwrap(
        await client.GET("/api/workloads/{workload_id}/observations", {
          params: { path: { workload_id: id }, query: { limit, offset: 0 } },
        }),
      ),
  });
}

export function useWorkloadObservationHistory(id: string, limit = 5) {
  return useQuery(observationHistoryQueryOptions(id, limit));
}

export function useRequestObservationRefresh(id: string) {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workloads/{workload_id}/observation-refresh", {
          params: { path: { workload_id: id } },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: workloadKeys.observation(id) }),
        cache.invalidateQueries({ queryKey: workloadKeys.observations(id) }),
      ]);
    },
  });
}
