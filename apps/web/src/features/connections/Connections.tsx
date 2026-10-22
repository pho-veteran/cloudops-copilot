import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";

export function Connections() {
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
        availability does not establish live source health.
      </p>
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
    </section>
  );
}
