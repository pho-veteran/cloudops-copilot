import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Failure, Loading } from "@/app/Feedback";
import { useKnowledgeStatus } from "./queries";

export function KnowledgeStatus() {
  const query = useKnowledgeStatus();
  return (
    <article className="foundation-panel">
      <h2>Knowledge index</h2>
      {query.isPending && <Loading />}
      {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
      {query.data && (
        <>
          <Badge variant="outline">
            {query.data.state.replaceAll("_", " ")}
          </Badge>
          <p>Full-text catalog · Semantic retrieval not configured</p>
          <dl className="foundation-metadata">
            <dt>Active generation</dt>
            <dd className="mono">
              {query.data.active_generation?.id ?? "Not indexed"}
            </dd>
            <dt>Last checked</dt>
            <dd>
              {query.data.last_checked_at
                ? new Date(query.data.last_checked_at).toLocaleString()
                : "Not checked"}
            </dd>
            <dt>Last success</dt>
            <dd>
              {query.data.last_success_at
                ? new Date(query.data.last_success_at).toLocaleString()
                : "No successful index"}
            </dd>
            <dt>Retained history</dt>
            <dd>
              {query.data.retained.document_versions} document versions ·{" "}
              {query.data.retained.chunks} chunks
            </dd>
          </dl>
          {query.data.last_attempt?.reason && (
            <p>{query.data.last_attempt.reason}</p>
          )}
          {query.data.state === "degraded" && (
            <p role="status">
              The last successful catalog and historical references remain
              readable. A trusted operator must inspect and retry the index
              update.
            </p>
          )}
          {query.data.state === "updating" && (
            <p role="status">
              An index update is in progress; reads use the last successful
              generation.
            </p>
          )}
          <p className="muted">
            Status reflects the last checked source snapshot. Corpus updates use
            the trusted maintenance CLI.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            Refresh index status
          </Button>
        </>
      )}
    </article>
  );
}
