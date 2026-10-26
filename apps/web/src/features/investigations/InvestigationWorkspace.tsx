import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { useIncident } from "@/features/incidents/queries";
import { runKeys, useRun, useRuns } from "./queries";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Failure, Loading } from "@/app/Feedback";
import { IncidentActivity } from "@/features/incidents/IncidentActivity";
import { RecoveryPlans } from "@/features/recovery/RecoveryPlans";
import { WorkloadIdentity } from "@/features/workloads/WorkloadIdentity";

export function InvestigationWorkspace({
  id,
  selectedRun,
  canMutate,
}: {
  id: string;
  selectedRun: string;
  canMutate: boolean;
}) {
  const incident = useIncident(id);
  const [offset, setOffset] = useState(0);
  const latest = useRuns(id);
  const history = useRuns(id, offset);
  const runId = selectedRun || latest.data?.items[0]?.id || "";
  const current = useRun(id, runId);
  const cache = useQueryClient();
  const requestKey = useRef(crypto.randomUUID());
  const mutation = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/incidents/{incident_id}/runs", {
          params: {
            path: { incident_id: id },
            header: { "idempotency-key": requestKey.current },
          },
        }),
      ),
    onSuccess: async (result) => {
      requestKey.current = crypto.randomUUID();
      await cache.invalidateQueries({ queryKey: runKeys.all(id) });
      window.location.hash = `/incidents/${id}?run=${result.run_id}`;
    },
  });
  if (incident.isPending || latest.isPending) return <Loading />;
  if (incident.isError)
    return (
      <Failure error={incident.error} retry={() => void incident.refetch()} />
    );
  if (latest.isError)
    return <Failure error={latest.error} retry={() => void latest.refetch()} />;
  const busy = latest.data.items.some(
    (r) => r.state === "queued" || r.state === "collecting",
  );
  return (
    <section>
      <a href="#/queue">← Work queue</a>
      <div className="eyebrow mt-5">
        INCIDENT · {incident.data.state.toUpperCase()}
      </div>
      <h1>{incident.data.title}</h1>
      <p className="muted">
        {incident.data.workload.namespace}/{incident.data.workload.name} ·{" "}
        {incident.data.id}
      </p>
      {incident.data.description && (
        <p className="whitespace-pre-wrap">{incident.data.description}</p>
      )}
      <WorkloadIdentity workload={incident.data.workload} />
      <div className="foundation-actions">
        {canMutate ? (
          <Button
            disabled={
              busy || mutation.isPending || !incident.data.workload.active
            }
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Starting…" : "Start new investigation"}
          </Button>
        ) : (
          <p className="muted">Viewer access · read-only</p>
        )}
        {busy && (
          <p className="muted">An investigation is queued or collecting.</p>
        )}
        {!incident.data.workload.active && (
          <p className="muted">
            This workload identity no longer admits new work. Its history remains readable; inspect the identity for its retirement status.
          </p>
        )}
      </div>
      {mutation.isError && <Failure error={mutation.error} />}
      <div className="foundation-workspace">
        <aside className="foundation-panel">
          <h2>Run history</h2>
          {history.isError ? (
            <Failure
              error={history.error}
              retry={() => void history.refetch()}
            />
          ) : history.isPending ? (
            <Loading />
          ) : (
            <>
              {!history.data.items.length && <p>No investigations saved.</p>}
              <ol className="foundation-stack">
                {history.data.items.map((r) => (
                  <li key={r.id}>
                    <a
                      aria-current={r.id === runId ? "page" : undefined}
                      href={`#/incidents/${id}?run=${r.id}`}
                    >
                      <span>{r.state.replaceAll("_", " ")}</span>
                      <br />
                      <small>{new Date(r.created_at).toLocaleString()}</small>
                      <br />
                      <small className="mono">{r.id}</small>
                    </a>
                  </li>
                ))}
              </ol>
              <div className="foundation-actions">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - 25))}
                >
                  Newer
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={history.data.next_offset === null}
                  onClick={() => setOffset(history.data!.next_offset!)}
                >
                  Older
                </Button>
              </div>
            </>
          )}
        </aside>
        <div className="foundation-stack" aria-live="polite">
          {!runId ? (
            <p>No investigation selected.</p>
          ) : current.isPending ? (
            <Loading />
          ) : current.isError ? (
            <Failure
              error={current.error}
              retry={() => void current.refetch()}
            />
          ) : (
            <>
              <article className="foundation-panel">
                <Badge variant="outline">
                  {current.data.state.replaceAll("_", " ")}
                </Badge>
                <h2>Investigation</h2>
                <p className="mono">{current.data.id}</p>
                <p>{current.data.reason}</p>
                <p className="muted">
                  AI diagnosis unavailable. Live manual recovery, when
                  configured, is reviewed separately below.
                </p>
                {current.data.state === "collecting" && (
                  <p className="muted">
                    If the worker stops, another worker can reclaim collection
                    after the 30-second lease. Obsolete attempts cannot publish.
                  </p>
                )}
                <p className="muted">
                  Created: {new Date(current.data.created_at).toLocaleString()}
                  <br />
                  Finished:{" "}
                  {current.data.finished_at
                    ? new Date(current.data.finished_at).toLocaleString()
                    : "Not finished"}
                </p>
              </article>
              <article className="foundation-panel">
                <h2>Evidence</h2>
                {!current.data.evidence.length ? (
                  <p>
                    No evidence has been published for this run. A failed
                    collection is not successful evidence.
                  </p>
                ) : (
                  current.data.evidence.map((e) => (
                    <div key={e.id} className="foundation-stack">
                      <Badge variant="outline">
                        Synthetic development fixture
                      </Badge>
                      <p>{e.payload.label}</p>
                      <p>{e.payload.observation}</p>
                      <dl className="foundation-metadata">
                        <dt>Source</dt>
                        <dd>{e.source}</dd>
                        <dt>Workload</dt>
                        <dd>{e.payload.workload}</dd>
                        <dt>UID</dt>
                        <dd className="mono">{e.workload_uid}</dd>
                        <dt>Observed</dt>
                        <dd>{new Date(e.observed_at).toLocaleString()}</dd>
                        <dt>Collected</dt>
                        <dd>{new Date(e.collected_at).toLocaleString()}</dd>
                        <dt>Evidence ID</dt>
                        <dd className="mono">{e.id}</dd>
                        <dt>SHA-256</dt>
                        <dd className="mono">{e.sha256}</dd>
                      </dl>
                      <details>
                        <summary>
                          Inspect stored payload · schema v{e.schema_version}
                        </summary>
                        <pre>{JSON.stringify(e.payload, null, 2)}</pre>
                      </details>
                    </div>
                  ))
                )}
              </article>
              <article className="foundation-panel">
                <h2>Collection attempts</h2>
                <p className="muted">
                  At most 3 attempts per run. A new investigation preserves all
                  earlier records.
                </p>
                {!current.data.attempts.length ? (
                  <p>Waiting for the worker to claim this run.</p>
                ) : (
                  <ol className="foundation-stack">
                    {current.data.attempts.map((a) => (
                      <li key={a.number}>
                        <strong>
                          Attempt {a.number} · {a.state}
                        </strong>
                        <p className="muted">
                          {new Date(a.started_at).toLocaleString()}
                          {a.finished_at &&
                            ` → ${new Date(a.finished_at).toLocaleString()}`}
                        </p>
                        {a.error && <p>{a.error}</p>}
                      </li>
                    ))}
                  </ol>
                )}
              </article>
            </>
          )}
        </div>
      </div>
      <IncidentActivity
        id={id}
        state={incident.data.state}
        canMutate={canMutate}
      />
      <RecoveryPlans
        id={id}
        canMutate={canMutate}
        live={incident.data.workload.source === "live_kubernetes"}
      />
    </section>
  );
}
