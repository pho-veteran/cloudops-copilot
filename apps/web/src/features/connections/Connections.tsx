import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { buildReportHash } from "@/app/routes";
import type { components } from "@/lib/api/generated/schema";
import { Badge } from "@/components/ui/badge";
import { AlertReceipts } from "./AlertReceipts";
import { OperationsPanel } from "./OperationsPanel";
import { SourceChecks } from "./SourceChecks";
import { KnowledgeStatus } from "@/features/knowledge/KnowledgeStatus";
import { ModelConnection } from "@/features/diagnosis/ModelConnection";

const observationLabels = {
  database: "Database",
  worker: "Worker",
  collector: "Collector",
  kubernetes: "Kubernetes observation",
  diagnosis: "Diagnosis",
  executor: "Executor",
  krr_runner: "KRR runner",
  krr_report: "Resource report history",
};

type Observation = components["schemas"]["HealthObservation"];

function ObservationCard({ observation, elapsed }: { observation: Observation; elapsed: number }) {
  const row = observation;
  const age = row.observed_at ? Date.parse(row.assessed_at) - Date.parse(row.observed_at) + elapsed : null;
  const aged = row.status === "fresh" && age !== null && row.stale_after_seconds != null &&
    age > row.stale_after_seconds * 1000;
  const workload = row.target?.workload_id;
  return (
    <article className="foundation-panel min-w-0">
      <h3>{observationLabels[row.component]}</h3>
      <Badge variant="outline">{(aged ? "stale" : row.status).replaceAll("_", " ")}</Badge>
      <p>{aged ? "The last observation is past its freshness threshold. Refresh runtime status." : row.reason}</p>
      {row.target && (
        <p className="muted break-all">
          {row.target.cluster} / {row.target.namespace} / {row.target.name}
          {row.target.uid && ` · UID ${row.target.uid}`}
        </p>
      )}
      {!!row.configured_adapters?.length && (
        <p className="muted">
          Adapters: {row.configured_adapters.map(adapter => adapter.replaceAll("_", " ")).join(", ")}
        </p>
      )}
      <p className="muted">Source: {row.source.replaceAll("_", " ")}</p>
      <p className="muted">
        Observed: {row.observed_at ? new Date(row.observed_at).toLocaleString() : "Not observed"}
      </p>
      <p className="muted">
        Last success: {row.last_success_at ? new Date(row.last_success_at).toLocaleString() : "Not recorded"}
      </p>
      {row.last_attempt_at && (
        <p className="muted">Last attempt: {new Date(row.last_attempt_at).toLocaleString()}</p>
      )}
      {row.last_failure_at && (
        <p className="muted">Last failure: {new Date(row.last_failure_at).toLocaleString()}</p>
      )}
      {row.component === "executor" && row.status === "see_feature" && (
        <a href="#/workloads">Open Workloads</a>
      )}
      {row.component === "kubernetes" && row.source === "live_kubernetes" && workload && (
        <a href={`#/workloads?workload=${workload}`}>Open observed workload</a>
      )}
      {row.report_id && (
        <a href={buildReportHash({ workload: workload ?? undefined, report: row.report_id })}>Open retained resource report</a>
      )}
    </article>
  );
}

export function Connections({ canMutate }: { canMutate: boolean }) {
  const query = useQuery({
    queryKey: ["health"],
    queryFn: async () => unwrap(await api.GET("/api/health")),
    refetchInterval: 10000,
  });
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const elapsed = Math.max(0, now - query.dataUpdatedAt);

  return (
    <section className="foundation-stack">
      <h1>Connections</h1>
      <section className="foundation-stack" aria-labelledby="runtime-status">
        <h2 id="runtime-status">Runtime and retained evidence</h2>
        {query.isPending && <Loading />}
        {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
        {query.data && (
          <>
            <p>
              Collection dispatch: {query.data.collection_dispatch.status}
              {query.data.collection_dispatch.reason_code &&
                ` · ${query.data.collection_dispatch.reason_code.replaceAll("_", " ")}`}
            </p>
            {query.isError && <p role="status">Showing the last saved assessment.</p>}
            <div className="foundation-grid">
              {query.data.observations.map(observation => (
                <ObservationCard key={observation.component} observation={observation} elapsed={elapsed} />
              ))}
            </div>
          </>
        )}
      </section>
      <SourceChecks canMutate={canMutate} />
      <ModelConnection canMutate={canMutate} />
      <OperationsPanel />
      <AlertReceipts />
      <KnowledgeStatus />
    </section>
  );
}
