import { useState } from "react";
import { useWorkloads } from "./queries";
import { WorkloadIdentity } from "./WorkloadIdentity";
import { Baselines } from "./Baselines";
import { ScopeStatus } from "./ScopeStatus";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function Workloads({
  canMutate,
  onInvestigate,
}: {
  canMutate: boolean;
  onInvestigate: (id: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const query = useWorkloads(offset);
  return (
    <section>
      <div className="eyebrow">CONFIGURED SCOPE</div>
      <h1>Workloads</h1>
      <p className="muted">
        Configured identities only. Inspect a fresh live candidate before
        confirming a known-good baseline; synthetic workloads cannot establish
        one.
      </p>
      <ScopeStatus />
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {!query.data.items.length && (
            <p>No configured workloads. Run the workload sync command.</p>
          )}
          <div className="foundation-grid">
            {query.data.items.map((w) => (
              <article key={w.id} className="foundation-panel">
                <Badge variant="outline">
                  {w.source === "live_kubernetes"
                    ? "Live Kubernetes identity"
                    : "Development fixture"}
                </Badge>
                <h2>
                  {w.namespace}/{w.name}
                </h2>
                <p>Cluster: {w.cluster}</p>
                <p className="muted">Live health unavailable</p>
                <WorkloadIdentity workload={w} />
                {w.source === "live_kubernetes" && (
                  <Baselines id={w.id} canMutate={canMutate} />
                )}
                {canMutate ? (
                  <Button onClick={() => onInvestigate(w.id)}>
                    Investigate workload
                  </Button>
                ) : (
                  <p className="muted">Viewer access · read-only</p>
                )}
              </article>
            ))}
          </div>
          <div className="foundation-actions">
            <Button
              variant="outline"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 25))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              disabled={query.data.next_offset === null}
              onClick={() => setOffset(query.data!.next_offset!)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
