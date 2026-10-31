import { queryOptions, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const workloadKeys = { all: ["workloads"] as const };
export function workloadsQueryOptions(offset = 0, client = api) {
  return queryOptions({
    queryKey: [...workloadKeys.all, offset],
    queryFn: async ({ signal }) =>
      unwrap(
        await client.GET("/api/workloads", {
          signal,
          params: { query: { limit: 25, offset } },
        }),
      ),
    refetchInterval: 15000,
    refetchIntervalInBackground: false,
  });
}

export function useWorkload(id?: string) {
  return useQuery({
    queryKey: [...workloadKeys.all, "identity", id],
    enabled: !!id,
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/workloads/{workload_id}", {
      signal, params: { path: { workload_id: id! } },
    })),
    refetchInterval: 15000,
    refetchIntervalInBackground: false,
  });
}

export function useObservation(id: string) {
  return useQuery({
    queryKey: [...workloadKeys.all, "observation", id],
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/workloads/{workload_id}/observation", {
      signal, params: { path: { workload_id: id } },
    })),
    refetchInterval: 15000,
    refetchIntervalInBackground: false,
  });
}

export function useObservationHistory(id: string, cursor?: string) {
  return useQuery({
    queryKey: [...workloadKeys.all, "history", id, cursor],
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/workloads/{workload_id}/observations", {
      signal, params: { path: { workload_id: id }, query: { limit: 25, cursor } },
    })),
  });
}
export function useWorkloads(offset = 0) {
  return useQuery(workloadsQueryOptions(offset));
}

export function scopeQueryOptions(offset = 0, removed = false, client = api) {
  return queryOptions({
    queryKey: [...workloadKeys.all, "scope", removed, offset],
    queryFn: async () => unwrap(await client.GET("/api/workloads/scope", {
      params: { query: { limit: 25, offset, removed } },
    })),
  });
}

export function identityQueryOptions(id: string, client = api) {
  return queryOptions({
    queryKey: [...workloadKeys.all, "identity", id],
    queryFn: async () => unwrap(await client.GET("/api/workloads/{workload_id}", {
      params: { path: { workload_id: id } },
    })),
  });
}
