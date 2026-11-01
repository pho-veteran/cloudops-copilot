import { useInfiniteQuery } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Receipt = components["schemas"]["AlertReceiptView"];

const outcomes: Record<Receipt["outcome"], string> = {
  accepted: "Accepted",
  deferred: "Accepted · waiting for capacity",
  duplicate: "Duplicate",
  resolved_signal: "Alert resolved",
  ignored: "Ignored",
  rejected: "Rejected",
  retryable: "Retry pending",
};

const reasons: Record<string, string> = {
  identity_missing:
    "The alert has no namespace with a deployment or pod label, or the object no longer exists.",
  identity_invalid: "Identity labels are not valid Kubernetes names.",
  identity_ambiguous: "Pod and deployment labels disagree or the owner chain is ambiguous.",
  identity_changed: "The live Deployment UID differs from the configured identity.",
  unsupported_identity: "The pod is not owned by a Deployment through a ReplicaSet.",
  out_of_scope: "The workload is not in the configured scope.",
  resolved_before_admission: "Resolved before any investigation was admitted.",
  scope_unpinned: "Intake needs the pinned live scope for its configured cluster.",
  cluster_unavailable: "The scoped cluster read failed; Alertmanager will retry.",
  budget_exhausted: "The scoped cluster read budget was exhausted; Alertmanager will retry.",
  capacity_exhausted: "Investigation capacity is full; Alertmanager will retry.",
  admission_conflict: "Admission was refused for now; Alertmanager will retry.",
  database_unavailable: "The application database was unavailable; Alertmanager will retry.",
};

export function describeReason(code: string | null) {
  if (!code) return null;
  return reasons[code] ?? `Unrecognized reason: ${code}`;
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
        Outcomes of authenticated Alertmanager deliveries, newest first. Unauthenticated or
        malformed deliveries are refused without being recorded. Intake never calls a model.
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
            <p>No authenticated deliveries yet.</p>
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
                      href={`#/incidents/${item.incident_id}${item.run_id ? `?run=${item.run_id}` : ""}`}
                    >
                      Open incident
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
