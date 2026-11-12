import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { KnowledgeRoute } from "@/app/routes";

export const knowledgeKeys = {
  all: ["knowledge"] as const,
  guidance: (incident: string, run: string) =>
    ["knowledge", "guidance", incident, run] as const,
};

export function useKnowledgeStatus() {
  return useQuery({
    queryKey: [...knowledgeKeys.all, "status"],
    queryFn: async () => unwrap(await api.GET("/api/knowledge/index-status")),
    refetchInterval: 10000,
  });
}

export function useKnowledgeCatalog(
  search = "",
  tags: string[] = [],
  cursor?: string,
  limit = 25,
) {
  const status = useKnowledgeStatus();
  return useQuery({
    queryKey: [
      ...knowledgeKeys.all,
      "catalog",
      search,
      tags,
      cursor,
      limit,
      status.data?.active_generation?.id,
    ],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/knowledge/documents", {
          params: { query: { q: search || undefined, tags, cursor, limit } },
        }),
      ),
    enabled: !status.isPending,
  });
}

export function useExactRunbook(route: KnowledgeRoute) {
  return useQuery({
    queryKey: [
      ...knowledgeKeys.all,
      "exact",
      route.document,
      route.chunk,
      route.incident,
      route.run,
      route.selection,
    ],
    enabled: Boolean(route.document),
    queryFn: async () => {
      if (route.incident && route.run && route.selection && route.chunk) {
        const target = unwrap(
          await api.GET(
            "/api/incidents/{incident_id}/runs/{run_id}/knowledge/selections/{selection_id}/chunks/{chunk_id}",
            {
              params: {
                path: {
                  incident_id: route.incident,
                  run_id: route.run,
                  selection_id: route.selection,
                  chunk_id: route.chunk,
                },
                query: { document_version_id: route.document! },
              },
            },
          ),
        );
        return { document: target.document, chunk: target.chunk };
      }
      const document = unwrap(
        await api.GET("/api/knowledge/documents/{document_version_id}", {
          params: { path: { document_version_id: route.document! } },
        }),
      );
      const chunk = route.chunk
        ? unwrap(
            await api.GET("/api/knowledge/chunks/{chunk_id}", {
              params: {
                path: { chunk_id: route.chunk },
                query: { document_version_id: route.document! },
              },
            }),
          )
        : null;
      return { document, chunk };
    },
  });
}
