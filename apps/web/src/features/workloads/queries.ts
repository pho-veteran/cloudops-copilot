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
