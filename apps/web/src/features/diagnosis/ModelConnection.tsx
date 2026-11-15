import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestKey, settleKey } from "@/features/investigations/request";
import { label } from "./Analysis";

export function ModelConnection({ canMutate }: { canMutate: boolean }) {
  const cache = useQueryClient();
  const status = useQuery({
    queryKey: ["model-status"],
    queryFn: async () => unwrap(await api.GET("/api/diagnosis/status")),
    refetchInterval: 3000,
    retry: false,
  });
  const checks = useQuery({
    queryKey: ["model-checks"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/diagnosis/checks", {
          params: { query: { limit: 10 } },
        }),
      ),
    refetchInterval: 3000,
    retry: false,
  });
  const check = useMutation({
    retry: false,
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/diagnosis/checks", {
          params: {
            header: { "idempotency-key": requestKey("model-check", {}) },
          },
          body: {},
        }),
      ),
    onSettled: (_data, error) => {
      settleKey("model-check", error ?? undefined);
      void cache.invalidateQueries({ queryKey: ["model-checks"] });
      void cache.invalidateQueries({ queryKey: ["model-status"] });
    },
  });
  return (
    <article
      className="foundation-panel foundation-stack min-w-0 break-words"
      aria-label="Model connection"
    >
      <h2>Model connection</h2>
      {status.isPending ? (
        <Loading />
      ) : status.isError ? (
        <Failure error={status.error} retry={() => void status.refetch()} />
      ) : (
        <>
          <Badge variant="outline">{label(status.data.availability)}</Badge>
          <p>
            Model: {status.data.identity.model || "Not configured"} · Endpoint:{" "}
            {status.data.identity.endpoint_label || "Not configured"}
          </p>
          <p>
            Quality qualification: {label(status.data.quality)}
            {status.data.qualification_cohort &&
              ` · ${status.data.qualification_cohort}`}
          </p>
          <p className="muted">
            A transport or schema probe does not establish diagnostic quality.
            Configuration and credentials are managed by the deployment
            operator.
          </p>
          <p>
            Dispatch:{" "}
            {status.data.dispatch_allowed
              ? "Allowed"
              : `Paused · ${label(status.data.dispatch_reason)}`}
          </p>
          <p>
            Last attempt: {status.data.last_attempt_at || "Not recorded"}
            <br />
            Last transport success:{" "}
            {status.data.last_success_at || "Not recorded"}
            <br />
            Last error: {label(status.data.last_error)}
          </p>
          {canMutate ? (
            <Button
              disabled={
                check.isPending ||
                [
                  "not_configured",
                  "configuration_invalid",
                  "disabled",
                ].includes(status.data.availability)
              }
              onClick={() => check.mutate()}
            >
              {check.isPending ? "Queuing check…" : "Check configured model"}
            </Button>
          ) : (
            <p>
              Viewer access: read stored checks. An Operator must initiate a
              check.
            </p>
          )}
          <details>
            <summary>Configuration identity</summary>
            <pre className="max-w-full overflow-auto whitespace-pre-wrap break-all">
              {JSON.stringify(status.data.identity, null, 2)}
            </pre>
          </details>
        </>
      )}
      {check.isError && <Failure error={check.error} />}
      {checks.isError ? (
        <Failure error={checks.error} retry={() => void checks.refetch()} />
      ) : checks.data?.items.length ? (
        <ul className="foundation-stack">
          {checks.data.items.map((item) => {
            const receipt = item.receipt ?? {};
            const usage =
              receipt.usage && typeof receipt.usage === "object"
                ? (receipt.usage as Record<string, unknown>)
                : {};
            return (
              <li key={item.id}>
                <p>
                  Check {item.id} · {label(item.state)}
                </p>
                <p>
                  Transport:{" "}
                  {receipt.transport_success === true
                    ? "Succeeded"
                    : receipt.transport_success === false
                      ? "Failed"
                      : "Not recorded"}{" "}
                  · Schema:{" "}
                  {receipt.schema_success === true
                    ? "Valid probe"
                    : receipt.schema_success === false
                      ? "Not validated"
                      : "Not recorded"}{" "}
                  · Observed total tokens:{" "}
                  {typeof usage.total_tokens === "number"
                    ? usage.total_tokens
                    : "Unavailable"}
                </p>
                <p className="muted">
                  Created: {item.created_at}. This check does not qualify
                  diagnosis.
                </p>
              </li>
            );
          })}
        </ul>
      ) : (
        <p>No model checks recorded.</p>
      )}
      <p className="muted">
        Only the latest ten stored checks are shown. Missing usage and remaining
        quota are unavailable.
      </p>
    </article>
  );
}
