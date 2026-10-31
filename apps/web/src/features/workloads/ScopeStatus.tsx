import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Failure, Loading } from "@/app/Feedback";
import { identityQueryOptions, scopeQueryOptions } from "./queries";
import { WorkloadIdentity } from "./WorkloadIdentity";

function SavedIdentity({ id }: { id: string }) {
  const query = useQuery(identityQueryOptions(id));
  if (query.isPending) return <Loading />;
  if (query.isError)
    return <Failure error={query.error} retry={() => void query.refetch()} />;
  return <WorkloadIdentity workload={query.data} />;
}

function TargetStatus({ target }: {
  target: components["schemas"]["ScopeTargetView"];
}) {
  const [inspect, setInspect] = useState(false);
  const reason = {
    not_found: "The configured Deployment was not found during synchronization.",
    permission_denied: "The scoped reader could not access this Deployment.",
    cluster_unavailable: "The cluster could not supply a valid observation.",
    budget_exhausted: "The synchronization read budget was exhausted.",
  };
  const label = {
    current: "Identity resolved",
    missing: "Configured · missing",
    removed: "Removed from scope",
    unavailable: "Identity observation unavailable",
  };
  return (
    <article className="foundation-panel">
      <Badge variant="outline">{label[target.state]}</Badge>
      <h3>{target.namespace}/{target.name}</h3>
      <p>Cluster: {target.cluster}</p>
      {target.reason && <p>{reason[target.reason]}</p>}
      <p className="muted">
        Last attempt: {new Date(target.last_attempt_at).toLocaleString()}
      </p>
      <p className="muted">
        Last successful identity lookup: {target.last_success_at
          ? new Date(target.last_success_at).toLocaleString() : "Not observed"}.
        This does not establish workload health.
      </p>
      {target.workload_id ? (
        <>
          <p className="mono">Saved Deployment UID: {target.uid}</p>
          <Button variant="outline" aria-expanded={inspect}
            onClick={() => setInspect(!inspect)}>
            {inspect ? "Hide saved identity" : "Inspect saved identity"}
          </Button>
          {inspect && <SavedIdentity id={target.workload_id} />}
        </>
      ) : (
        <p>No saved identity. Investigation and baseline preparation require a resolved workload.</p>
      )}
      {target.state !== "current" && (
        <p className="muted">
          A deployment operator can inspect the host policy and run synchronization again.
          Previous observations remain historical; synchronization grants no execution authority.
        </p>
      )}
    </article>
  );
}

export function ScopeStatus() {
  const [removed, setRemoved] = useState(false);
  const [offset, setOffset] = useState(0);
  const query = useQuery(scopeQueryOptions(offset, removed));
  return (
    <section aria-label="Live scope synchronization">
      <h2>Live scope synchronization</h2>
      <p className="muted">
        Configured names and their last identity lookup.
        Legacy UID-pinned entries remain in the workload inventory.
      </p>
      <div className="foundation-actions">
        <Button variant="outline" aria-pressed={removed}
          onClick={() => { setRemoved(!removed); setOffset(0); }}>
          {removed ? "Show current scope" : "Show removed targets"}
        </Button>
        <Button variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>
          Refresh saved status
        </Button>
      </div>
      {query.isPending ? <Loading /> : query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : (
        <>
          {!query.data.items.length && (
            <p>{removed ? "No removed live targets."
              : "No versioned live scope records. UID-pinned and fixture entries are shown below."}</p>
          )}
          <div className="foundation-grid">
            {query.data.items.map(target => <TargetStatus key={target.id} target={target} />)}
          </div>
          <div className="foundation-actions">
            <Button variant="outline" disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 25))}>
              Previous scope page
            </Button>
            <Button variant="outline" disabled={query.data.next_offset === null}
              onClick={() => setOffset(query.data!.next_offset!)}>
              Next scope page
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
