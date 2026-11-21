import { useInfiniteQuery } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { buildIncidentHash } from "@/app/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Receipt = components["schemas"]["AlertReceiptView"];

const outcomes: Record<Receipt["outcome"], string> = {
  accepted: "Accepted",
  deferred: "Accepted · deferred at delivery",
  duplicate: "Duplicate",
  resolved_signal: "Alert resolved",
  ignored: "Ignored",
  rejected: "Rejected",
  retryable: "Retry requested at delivery",
};

const reasons: Record<string, string> = {
  identity_missing:
    "The workload identity could not be found. Check the namespace and deployment or pod labels, and confirm the object still exists.",
  identity_invalid: "Identity labels are invalid. Use valid Kubernetes names in the alert labels.",
  identity_ambiguous: "The workload identity is ambiguous. Check that the pod and deployment labels agree and the owner chain identifies one Deployment.",
  identity_changed: "The live Deployment UID differs from the configured identity. Ask the scope owner to review the pinned UID.",
  unsupported_identity: "The pod is not owned by a Deployment through a ReplicaSet. Check the workload type and alert mapping.",
  out_of_scope: "The workload is not in the configured scope. Check the alert's cluster, namespace and workload against the pinned scope.",
  resolved_before_admission: "Resolved before any investigation was admitted.",
  scope_unpinned: "Intake needs a pinned live scope. Ask the scope owner to check the configured cluster and scope.",
  scope_changed: "The workload scope changed during the read.",
  cluster_unavailable: "The scoped cluster read failed.",
  budget_exhausted: "The scoped cluster read budget was exhausted.",
  capacity_exhausted: "Investigation capacity was full.",
  admission_conflict: "Admission was temporarily refused.",
  database_unavailable: "The application database was unavailable.",
};

export function describeReason(code: string | null) {
  if (!code) return null;
  return Object.hasOwn(reasons, code)
    ? reasons[code]
    : "Reason unavailable. Check the delivery configuration or ask the intake owner.";
}

export function AlertReceipts() {
  const query = useInfiniteQuery({
    queryKey: ["alert-receipts"],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/alert-receipts", {
          params: { query: pageParam ? { cursor: pageParam } : {} },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
  });
  const first = query.data?.pages[0];
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const refetch = () => void query.refetch();
  const fetchOlder = () => void query.fetchNextPage();
  // A failed refresh or older page keeps the receipts already shown.
  const laterError = query.data && query.isError ? query.error : null;
  return (
    <section className="foundation-panel foundation-stack" aria-labelledby="alert-intake">
      <h2 id="alert-intake">Alert intake</h2>
      <p className="muted">
        Retained outcomes of authenticated Alertmanager deliveries, newest first. Each receipt
        records the outcome at delivery time.
      </p>
      {query.isPending ? (
        <Loading />
      ) : !query.data ? (
        <Failure error={query.error} retry={refetch} />
      ) : (
        <>
          <p>
            {first?.configured
              ? `Intake configured · source ${first.source_id}`
              : "Alert intake is not configured. Deliveries are refused without writes."}
          </p>
          <div className="foundation-actions">
            <Button variant="outline" disabled={query.isFetching} onClick={refetch}>
              {query.isFetching && !query.isFetchingNextPage ? "Refreshing…" : "Refresh receipts"}
            </Button>
          </div>
          {!items.length ? (
            <p>No retained deliveries.</p>
          ) : (
            <ol className="foundation-stack">
              {items.map((item) => (
                <li key={item.id} className="foundation-receipt">
                  <Badge variant="outline">{outcomes[item.outcome] ?? item.outcome}</Badge>{" "}
                  <strong>{item.alertname ?? "Unnamed alert"}</strong>{" "}
                  <span className="muted">
                    {item.alert_status} ·{" "}
                    {item.namespace
                      ? `${item.namespace}/${item.target_name ?? "no target label"}`
                      : "no namespace label"}
                    {item.target_kind ? ` (${item.target_kind})` : ""}
                  </span>
                  <br />
                  <small title={item.received_at}>
                    {new Date(item.received_at).toLocaleString()}
                  </small>
                  {describeReason(item.reason) && <p>{describeReason(item.reason)}</p>}
                  {item.incident_id && (
                    <a
                      href={buildIncidentHash(item.incident_id, item.run_id ?? undefined)}
                    >
                      {item.run_id ? "Open Run" : "Open incident"}
                    </a>
                  )}
                </li>
              ))}
            </ol>
          )}
          {laterError && (
            <Failure
              error={laterError}
              retry={query.isFetchNextPageError ? fetchOlder : refetch}
            />
          )}
          {query.hasNextPage && (
            <Button
              variant="outline"
              disabled={query.isFetchingNextPage}
              onClick={fetchOlder}
            >
              {query.isFetchingNextPage ? "Loading…" : "Load older receipts"}
            </Button>
          )}
        </>
      )}
    </section>
  );
}
