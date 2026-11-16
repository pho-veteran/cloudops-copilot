import { useState } from "react";
import { useIncidents } from "@/features/incidents/queries";
import { useObservation, useObservationHistory } from "./queries";
import { useAgedObservation, WorkloadHealth } from "./WorkloadHealth";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import type { components } from "@/lib/api/generated/schema";
import { RecoveryAuthority } from "@/features/recovery/RecoveryEvidence";

function RelatedIncidents({ id }: { id: string }) {
  const [offset, setOffset] = useState(0);
  const query = useIncidents(offset, { workload_id: id, view: "all", sort_by: "created_at" });
  return <section aria-label="Related incidents" className="foundation-stack">
    <h3>Related incidents</h3>
    <p className="muted">Records for this exact workload UUID, including historical investigations.</p>
    {query.isPending ? <Loading /> : query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : <>
      {!query.data.items.length && <p>No related incidents.</p>}
      {query.data.items.map(item => <a key={item.id} href={`#/incidents/${item.id}`} className="foundation-panel break-words">
        {item.title} · {item.state}<br /><span className="mono">{item.id}</span>
      </a>)}
      <div className="foundation-actions">
        <Button variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous incidents</Button>
        <Button variant="outline" disabled={query.data.next_offset === null} onClick={() => setOffset(query.data.next_offset!)}>Next incidents</Button>
      </div>
    </>}
  </section>;
}

function Evidence({ value, historical }: { value: components["schemas"]["PublicObservation"]; historical: boolean }) {
  const partial = value.coverage === "partial";
  return <section aria-label={partial ? "Partial Kubernetes evidence" : historical ? "Historical Kubernetes evidence" : "Observed Kubernetes evidence"} className="foundation-stack">
    <h3>{partial ? "Latest read · partial coverage" : historical ? "Historical evidence · not current health" : "Observed Kubernetes evidence"}</h3>
    {partial && <p className="muted">Partial evidence does not establish current health. Inspect its read time and the freshness summary.</p>}
    <p className="mono break-all">Observation: {value.observation_id}</p>
    <p>{new Date(value.started_at).toLocaleString()} → {new Date(value.observed_at).toLocaleString()}</p>
    <p>Generation {value.deployment.generation} · Ready {value.deployment.ready ?? "unknown"} / {value.deployment.desired} · {value.coverage}</p>
    <p className="mono break-all">Current ReplicaSet UID: {value.current_replicaset_uid ?? "Unknown"}</p>
    <h4>Owned Pods and containers</h4>
    {!value.pods.length && <p>No owned Pods in this read window.</p>}
    {value.pods.map(pod => <article key={pod.uid} className="foundation-panel min-w-0 break-words">
      <strong>{pod.name}</strong><p className="mono break-all">{pod.uid}</p>
      <p>{pod.phase} · {pod.current ? "Current ReplicaSet" : "Old ReplicaSet"} · Ready {pod.ready === null ? "unknown" : String(pod.ready)}{pod.excluded ? " · Excluded from positive coverage" : ""}</p>
      <ul>{pod.containers.map(container => <li key={`${container.kind}:${container.name}`}>
        {container.kind} / {container.name} · {container.state} · restarts {container.restart_count ?? "unknown"}{container.reason ? ` · ${container.reason.replaceAll("_", " ")}` : ""}
      </li>)}</ul>
    </article>)}
    <p className="muted">Unrelated Pods excluded: {value.unrelated_pods}. {value.limitations}</p>
  </section>;
}

export function WorkloadContext({ id }: { id: string }) {
  const query = useObservation(id);
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const history = useObservationHistory(id, cursors.at(-1));
  const aged = useAgedObservation(query.data?.summary);
  return <div className="foundation-stack min-w-0 break-words">
    {query.isPending ? <Loading /> : query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : <>
      <WorkloadHealth summary={query.data.summary} />
      <Button variant="outline" disabled={query.isFetching || history.isFetching} onClick={() => void Promise.all([query.refetch(), history.refetch()])}>Refresh displayed data</Button>
      <p className="muted">Refresh reads saved observations. The scoped observer collects Kubernetes state separately.</p>
      <dl className="foundation-metadata">
        <dt>Last attempt</dt><dd>{query.data.summary.last_attempt_at ?? "Not attempted"}</dd>
        <dt>Last success</dt><dd>{query.data.summary.last_success_at ?? "None"}</dd>
        <dt>Last failure</dt><dd>{query.data.summary.last_failure_at ?? "None"} {query.data.summary.last_failure_reason?.replaceAll("_", " ")}</dd>
      </dl>
      {query.data.current && <Evidence value={query.data.current} historical={aged.health === "unknown"} />}
      {query.data.historical && <Evidence value={query.data.historical} historical />}
      {!query.data.current && !query.data.historical && <p>No retained observation evidence. Start the configured observer or retry after a successful read.</p>}
      <section aria-label="Observed changes">
        <h3>Observed changes</h3>
        <p className="muted">Comparison of retained complete observations; historical when current health is unknown.</p>
        <p>Comparison: {query.data.comparison.state.replaceAll("_", " ")}. Measured restart delta: {query.data.comparison.restart_delta ?? "Unknown"}</p>
        <p>{query.data.comparison.changes?.length ? query.data.comparison.changes.map(v => v.replaceAll("_", " ")).join(" · ") : "No comparable selected-field changes recorded."}</p>
        {query.data.comparison.from_observation_id && <p className="mono break-all">{query.data.comparison.from_observation_id} → {query.data.comparison.to_observation_id}<br />Generation {query.data.comparison.from_generation} → {query.data.comparison.to_generation}<br />{query.data.comparison.from_at} → {query.data.comparison.to_at}</p>}
        <p className="muted">Only compared observations and whitelisted fields are covered. This is not causal diagnosis or unseen deployment history.</p>
      </section>
    </>}
    <section aria-label="Observation history" className="foundation-stack">
      <h3>Observation history</h3>
      {history.isPending ? <Loading /> : history.isError ? <Failure error={history.error} retry={() => void history.refetch()} /> : <>
        {!history.data.items.length && <p>No retained complete observations.</p>}
        <ul>{history.data.items.map(item => <li key={item.observation_id} className="break-all">{item.observed_at} · generation {item.generation} · {item.health.replaceAll("_", " ")} · {item.observation_id}</li>)}</ul>
        <div className="foundation-actions">
          <Button variant="outline" disabled={cursors.length === 1} onClick={() => setCursors(v => v.slice(0, -1))}>Previous observations</Button>
          <Button variant="outline" disabled={!history.data.next_cursor} onClick={() => setCursors(v => [...v, history.data.next_cursor!])}>Next observations</Button>
        </div>
      </>}
    </section>
    <RecoveryAuthority workloadId={id} />
    <RelatedIncidents id={id} />
  </div>;
}
