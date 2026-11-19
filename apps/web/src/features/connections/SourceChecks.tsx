import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestKey, settleKey } from "@/features/investigations/request";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";

type Component = components["schemas"]["CheckBody"]["component"];
type Metrics = components["schemas"]["MetricsCheck"];

const labels: Record<Component, string> = {
  cluster: "Kubernetes",
  prometheus: "Prometheus",
  database: "Database",
  worker: "Worker",
};
const checksKey = ["connection-checks"];
const statusKey = [...checksKey, "status"];

function time(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Unavailable";
}

function MetricsEvidence({ evidence }: { evidence: Metrics }) {
  return (
    <dl className="muted">
      <dt>Query succeeded</dt><dd>{time(evidence.query_succeeded_at)}</dd>
      <dt>Scoped coverage</dt><dd>{evidence.coverage}</dd>
      <dt>Raw sample freshness</dt><dd>{evidence.raw_freshness}</dd>
    </dl>
  );
}

function CheckButton({ component }: { component: Component }) {
  const cache = useQueryClient();
  const scope = `connection-check:${component}`;
  const check = useMutation({
    retry: false,
    mutationFn: async (key: string) => unwrap(await api.POST("/api/connection-checks", {
      body: { component },
      params: { header: { "idempotency-key": key } },
    })),
    onSuccess(receipt, key) {
      if (receipt.reason_code !== "check_pending") settleKey(scope, undefined, key);
    },
    onError(error, key) {
      settleKey(scope, error, key);
    },
    onSettled() {
      return cache.invalidateQueries({ queryKey: checksKey });
    },
  });

  return (
    <div>
      <Button variant="outline" disabled={check.isPending} onClick={() => check.mutate(requestKey(scope, {}))}>
        {`Check ${labels[component]} connection`}
      </Button>
      {check.isPending && <p role="status">Checking {labels[component]}…</p>}
      {check.isSuccess && <p role="status">{check.data.reason}</p>}
      {check.isError && <Failure error={check.error} />}
    </div>
  );
}

export function SourceChecks({ canMutate }: { canMutate: boolean }) {
  const statuses = useQuery({
    queryKey: statusKey,
    queryFn: async () => unwrap(await api.GET("/api/connection-checks/status")),
    refetchInterval: 30_000,
  });
  const history = useQuery({
    queryKey: checksKey,
    queryFn: async () => unwrap(await api.GET("/api/connection-checks")),
  });
  return (
    <section aria-label="Configured source checks" className="foundation-stack">
      {!canMutate && <p>Viewer access allows inspection of saved checks. An Operator can start a new check.</p>}
      {statuses.isPending && <Loading />}
      {statuses.isError && <Failure error={statuses.error} retry={() => void statuses.refetch()} />}
      <div className="foundation-grid">
        {(Object.keys(labels) as Component[]).map((component) => {
          const item = statuses.data?.find((entry) => entry.component === component);
          return (
            <article key={component} className="foundation-panel min-w-0">
              <h2>{labels[component]} check</h2>
              {item && <>
                <Badge variant="outline">{item.status.replaceAll("_", " ")}</Badge>
                <p>{item.reason}</p>
                {item.target && <p className="break-all">{item.target.cluster} / {item.target.namespace} / {item.target.name} · UID {item.target.uid}</p>}
                <dl className="muted">
                  <dt>Last attempt</dt><dd>{time(item.last_attempt_at)}</dd>
                  <dt>Observed</dt><dd>{time(item.observed_at)}</dd>
                  <dt>Last success</dt><dd>{time(item.last_success_at)}</dd>
                  <dt>Last failure</dt><dd>{time(item.last_failure_at)}</dd>
                </dl>
                {item.metrics && <MetricsEvidence evidence={item.metrics} />}
              </>}
              {canMutate && <CheckButton component={component} />}
            </article>
          );
        })}
      </div>
      <details>
        <summary>Check history</summary>
        {history.isPending && <Loading />}
        {history.isError && <Failure error={history.error} retry={() => void history.refetch()} />}
        {history.data?.length === 0 && <p>No checks have been recorded.</p>}
        {history.data?.map((item) => (
          <article key={item.id}>
            <p>{labels[item.component]} · {time(item.finished_at ?? item.observed_at)} · {item.reason}
              {item.historical && " Historical result."}</p>
            {item.target && <p className="break-all">{item.target.namespace} / {item.target.name} · UID {item.target.uid}</p>}
            {item.metrics && <MetricsEvidence evidence={item.metrics} />}
          </article>
        ))}
      </details>
    </section>
  );
}
