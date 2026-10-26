import { queryOptions, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";

export const workloadKeys = { all: ["workloads"] as const };
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
