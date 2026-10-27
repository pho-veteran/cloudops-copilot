import { useRef, useState } from "react";
import { useWorkloads, useWorkload } from "./queries";
import { WorkloadIdentity } from "./WorkloadIdentity";
import { Baselines } from "./Baselines";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { WorkloadHealth } from "./WorkloadHealth";
import { WorkloadContext } from "./WorkloadContext";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

export function Workloads({
  canMutate,
  onInvestigate,
  selectedWorkload,
}: {
  canMutate: boolean;
  onInvestigate: (id: string) => void;
  selectedWorkload?: string;
}) {
  const [offset, setOffset] = useState(0);
  const detailTrigger = useRef<HTMLButtonElement | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const query = useWorkloads(offset);
  const selected = useWorkload(selectedWorkload);
  return (
    <section>
      <div className="eyebrow">CONFIGURED SCOPE</div>
      <h1 ref={heading} tabIndex={-1}>Workloads</h1>
      <p className="muted">
        Configured identities only. Open workload context for owned Pods,
        observation limits, related incidents and known-good preparation.
      </p>
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
                <WorkloadHealth summary={w.observation} />
                <WorkloadIdentity workload={w} />
                <Button variant="outline" onClick={event => { detailTrigger.current = event.currentTarget; window.location.hash = `#/workloads?workload=${w.id}`; }}>View workload context</Button>
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
      <Dialog open={!!selectedWorkload} onOpenChange={open => { if (!open) window.location.hash = "#/workloads"; }}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl" onCloseAutoFocus={event => {
          event.preventDefault();
          (detailTrigger.current?.isConnected ? detailTrigger.current : heading.current)?.focus();
        }}>
          <DialogHeader>
            <DialogTitle>{selected.data ? `${selected.data.namespace}/${selected.data.name}` : "Workload context"}</DialogTitle>
            <DialogDescription>Exact persisted identity, Kubernetes observations and related investigations.</DialogDescription>
          </DialogHeader>
          {selected.isPending ? <Loading /> : selected.isError ? <Failure error={selected.error} retry={() => void selected.refetch()} /> : selected.data && <>
            <WorkloadIdentity workload={selected.data} />
            <WorkloadContext key={selected.data.id} id={selected.data.id} />
            {selected.data.source === "live_kubernetes" && <Baselines id={selected.data.id} canMutate={canMutate && selected.data.active} />}
            {canMutate && selected.data.active && <Button onClick={() => onInvestigate(selected.data.id)}>Investigate workload</Button>}
          </>}
        </DialogContent>
      </Dialog>
    </section>
  );
}
