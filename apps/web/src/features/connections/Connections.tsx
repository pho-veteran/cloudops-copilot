import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertReceipts } from "./AlertReceipts";
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

export function Connections({ canMutate }: { canMutate: boolean }) {
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
      <SourceChecks canMutate={canMutate} />
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <div className="foundation-grid">
          <p>
            Collection dispatch: {query.data.collection_dispatch.status}
            {query.data.collection_dispatch.reason_code &&
              ` · ${query.data.collection_dispatch.reason_code.replaceAll("_", " ")}`}
          </p>
          {query.data.observations.map((o) => (
            <article className="foundation-panel" key={o.component}>
              <Badge variant="outline">{o.status.replaceAll("_", " ")}</Badge>
              <h2>{observationLabels[o.component]}</h2>
              <p>{o.reason}</p>
              {o.target && (
                <p className="muted break-all">
                  {o.target.cluster} / {o.target.namespace} / {o.target.name}
                  {o.target.uid && ` · UID ${o.target.uid}`}
                </p>
              )}
              {!!o.configured_adapters?.length && (
                <p className="muted">
                  Adapters: {o.configured_adapters.map((adapter) => adapter.replaceAll("_", " ")).join(", ")}
                </p>
              )}
              <p className="muted">Source: {o.source.replaceAll("_", " ")}</p>
              <p className="muted">
                Observed: {o.observed_at ? new Date(o.observed_at).toLocaleString() : "Not observed"}
              </p>
              <p className="muted">
                Last success:{" "}
                {o.last_success_at
                  ? new Date(o.last_success_at).toLocaleString()
                  : "Not recorded"}
              </p>
              {o.last_attempt_at && (
                <p className="muted">Last attempt: {new Date(o.last_attempt_at).toLocaleString()}</p>
              )}
              {o.last_failure_at && (
                <p className="muted">Last failure: {new Date(o.last_failure_at).toLocaleString()}</p>
              )}
              {o.component === "executor" && o.status === "see_feature" && (
                <a href="#/workloads">Open Workloads</a>
              )}
              {o.component === "kubernetes" && o.source === "live_kubernetes" && o.target?.workload_id && (
                <a href={`#/workloads?workload=${o.target.workload_id}`}>Open observed workload</a>
              )}
              {o.component === "krr_report" && o.report_id && (
                <a href="#/reports">Open Resource reports</a>
              )}
            </article>
          ))}
        </div>
      )}
      <AlertReceipts />
      <KnowledgeStatus />
      <ModelConnection canMutate={canMutate} />
    </section>
  );
}
