import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertReceipts } from "./AlertReceipts";
import { KnowledgeStatus } from "@/features/knowledge/KnowledgeStatus";

export function Connections({ canMutate }: { canMutate: boolean }) {
  const cache = useQueryClient();
  const checks = useQuery({
    queryKey: ["connection-checks"],
    queryFn: async () => unwrap(await api.GET("/api/connection-checks")),
  });
  const check = useMutation({
    mutationFn: async (component: "cluster" | "database" | "worker") =>
      unwrap(await api.POST("/api/connection-checks", { body: { component } })),
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: ["connection-checks"] }),
  });
  const query = useQuery({
    queryKey: ["health"],
    queryFn: async () => unwrap(await api.GET("/api/health")),
    refetchInterval: 10000,
  });
  return (
    <section>
      <div className="eyebrow">LOCAL OPERATING STATUS</div>
      <h1>Connections</h1>
      <p className="muted">
        A fresh worker heartbeat describes the local process. Fixture
        availability does not establish live source health. Collector
        availability describes the configured adapter; inspect investigation
        attempts for collection results.
      </p>
      {canMutate ? (
        <div className="foundation-actions">
          {(["cluster", "database", "worker"] as const).map((component) => (
            <Button
              key={component}
              variant="outline"
              disabled={check.isPending}
              onClick={() => check.mutate(component)}
            >
              Check {component} connection
            </Button>
          ))}
        </div>
      ) : (
        <p>
          Viewer access: inspect existing observations. An Operator must
          initiate a connection check.
        </p>
      )}
      {check.isError && <Failure error={check.error} />}
      {checks.isError && <Failure error={checks.error} />}
      {checks.data?.map((item) => (
        <p key={item.id}>
          {item.component} · {item.status} ·{" "}
          {new Date(item.observed_at).toLocaleString()} · {item.reason}
        </p>
      ))}
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <div className="foundation-grid">
          {query.data.observations.map((o) => (
            <article className="foundation-panel" key={o.component}>
              <Badge variant="outline">{o.status}</Badge>
              <h2>{o.component}</h2>
              <p>{o.reason}</p>
              <p className="muted">Source: {o.source.replaceAll("_", " ")}</p>
              <p className="muted">
                Observed: {new Date(o.observed_at).toLocaleString()}
              </p>
              <p className="muted">
                Last success:{" "}
                {o.last_success_at
                  ? new Date(o.last_success_at).toLocaleString()
                  : "Unavailable"}
              </p>
            </article>
          ))}
        </div>
      )}
      <AlertReceipts />
      <KnowledgeStatus />
    </section>
  );
}
