import { useState } from "react";
import { useIncidents } from "./queries";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function WorkQueue() {
  const [offset, setOffset] = useState(0);
  const query = useIncidents(offset);
  return (
    <section>
      <div className="eyebrow">PERSISTED APPLICATION DATA</div>
      <h1>Work queue</h1>
      <p className="muted">Manual incidents saved in your local workspace.</p>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {!query.data.items.length && (
            <div className="foundation-panel">
              <h2>No incidents yet</h2>
              <p>Select a development workload and start an investigation.</p>
              <a href="#/workloads">View workloads</a>
            </div>
          )}
          <div className="foundation-stack">
            {query.data.items.map((incident) => (
              <a
                className="foundation-panel foundation-incident"
                key={incident.id}
                href={`#/incidents/${incident.id}`}
              >
                <div>
                  <strong>{incident.title}</strong>
                  <p className="muted">
                    {new Date(incident.created_at).toLocaleString()} ·{" "}
                    {incident.id}
                  </p>
                </div>
                <Badge variant="outline">{incident.state}</Badge>
              </a>
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
