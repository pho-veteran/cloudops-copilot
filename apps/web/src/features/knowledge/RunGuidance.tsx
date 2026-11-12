import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { buildKnowledgeHash } from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { knowledgeKeys } from "./queries";

export function RunGuidance({
  incidentId,
  runId,
  canMutate,
}: {
  incidentId: string;
  runId: string;
  canMutate: boolean;
}) {
  const cache = useQueryClient();
  const key = knowledgeKeys.guidance(incidentId, runId);
  const query = useQuery({
    queryKey: key,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs/{run_id}/knowledge", {
          params: { path: { incident_id: incidentId, run_id: runId } },
        }),
      ),
  });
  const find = useMutation({
    retry: false,
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/incidents/{incident_id}/runs/{run_id}/knowledge", {
          params: { path: { incident_id: incidentId, run_id: runId } },
          body: {},
        }),
      ),
    onSuccess: (data) => cache.setQueryData(key, data),
  });
  const saved = query.data?.selection;
  const legacy = query.data?.reason === "legacy_request";
  return (
    <article className="foundation-panel">
      <h2>Saved runbook guidance</h2>
      <p className="muted">
        Retrieved methodology, separate from telemetry and AI diagnosis. Reading
        or finding guidance does not authorize recovery.
      </p>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : saved ? (
        <>
          <Badge variant="outline">Saved manual selection</Badge>
          <p>
            Saved {new Date(saved.created_at).toLocaleString()} · Full-text ·{" "}
            {saved.retrieval.status.replaceAll("_", " ")}
          </p>
          <p className="mono">Selection {saved.id}</p>
          <p className="whitespace-pre-wrap">Query: {saved.retrieval.query}</p>
          <p className="mono">Generation {saved.retrieval.generation_id}</p>
          {saved.retrieval.degraded && (
            <p role="status">This selection used a degraded last-good index.</p>
          )}
          {saved.retrieval.truncated && (
            <p role="status">
              Results were bounded:{" "}
              {saved.retrieval.reason?.replaceAll("_", " ")}.{" "}
              {saved.retrieval.content_bytes} bytes retained.
            </p>
          )}
          {saved.retrieval.status === "no_matches" && (
            <p>No matching guidance was found for this saved request.</p>
          )}
          <ul>
            {(saved.retrieval.limitations ?? []).map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
          <ol className="foundation-stack knowledge-catalog">
            {(saved.retrieval.chunks ?? []).map((item, index) => (
              <li key={item.reference.chunk_id}>
                <a
                  href={buildKnowledgeHash({
                    document: item.reference.document_version_id,
                    chunk: item.reference.chunk_id,
                    incident: incidentId,
                    run: runId,
                    selection: saved.id,
                    returnTo: window.location.hash,
                  })}
                >
                  {item.title} · version {item.reference.version} · result{" "}
                  {index + 1}
                </a>
                <p className="muted">
                  {item.heading_path.join(" / ") || "Document text"}
                </p>
              </li>
            ))}
          </ol>
          <p>
            A new investigation is needed for fresh manual guidance. This saved
            selection stays unchanged; the{" "}
            <a href="#/knowledge">current catalog</a> remains browsable.
          </p>
        </>
      ) : (
        <>
          <p>
            {legacy
              ? "The original question or scope was not captured. A new investigation is needed for guidance."
              : query.data?.state === "unavailable"
                ? "The index is not ready for guidance. A trusted operator must index the corpus; then retry explicitly."
                : "No guidance has been selected for this Run."}
          </p>
          {canMutate && !legacy && (
            <Button disabled={find.isPending} onClick={() => find.mutate()}>
              {find.isPending
                ? "Finding guidance…"
                : find.isError || query.data?.state === "unavailable"
                  ? "Retry Find guidance"
                  : "Find guidance"}
            </Button>
          )}
          {!canMutate && (
            <p className="muted">
              Viewer access: read saved guidance. An Operator must find
              guidance.
            </p>
          )}
        </>
      )}
      {find.isError && <Failure error={find.error} />}
    </article>
  );
}
