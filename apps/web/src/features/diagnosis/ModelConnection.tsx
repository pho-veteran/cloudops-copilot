import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestKey, settleKey } from "@/features/investigations/request";
import { label } from "./Analysis";

type ModelStatus = components["schemas"]["ModelStatus"];
type Check = components["schemas"]["cloudops__features__diagnosis__api_schemas__CheckView"];

function time(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : "Not recorded";
}

function nextStep(code: string | null | undefined) {
  switch (code) {
    case "text":
      return "The most recent model call completed at the recorded time; review the independent validation and qualification states above.";
    case "auth_denied":
      return "The deployment credential was rejected (HTTP 401/403). Check the configured credential reference and provider access; your app session remains active.";
    case "rate_limited":
      return "The provider rate limited this request (HTTP 429). Wait for the provider's limit window before an Operator starts another check.";
    case "timeout":
    case "transport_error":
    case "provider_outcome_unknown":
      return "The request outcome is unknown. Review provider availability and the recorded timestamp before an Operator starts a new check.";
    case "configuration_changed":
      return "The deployment profile or policy changed after this check was queued. Review the current configuration and start a new check if needed.";
    case "credential_unavailable":
    case "credential_invalid":
      return "The deployment credential reference could not be used. Check the deployment secret manager without entering a credential in this app.";
    case "configuration_invalid":
    case "destination_mismatch":
    case "unqualified":
      return "Review the deployment profile, matching egress policy and qualification identity. This panel cannot edit trusted deployment configuration.";
    default:
      return code
        ? `Review the deployment-side model configuration and the classified result (${label(code)}).`
        : "No provider error is recorded for the current configuration.";
  }
}

function usageText(status: ModelStatus) {
  const usage = status.latest_usage;
  if (!usage) return "No matching model call usage is recorded.";
  const amount =
    usage.status === "observed" && usage.total_tokens !== null
      ? `${usage.total_tokens} total tokens`
      : usage.status === "observed"
        ? "Usage observed; total tokens were not reported"
        : "Unavailable";
  return `${amount} · ${label(usage.source)} · observed ${time(usage.observed_at)}`;
}

function checkUsage(check: Check) {
  const receipt = check.receipt;
  const usage = receipt?.usage as Record<string, unknown> | null | undefined;
  if (receipt?.usage_status !== "observed" || !usage) return "Unavailable";
  return typeof usage.total_tokens === "number"
    ? `${usage.total_tokens} total tokens`
    : "Unavailable";
}

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
            Model: {status.data.identity.model || "Not configured"} · Destination: {" "}
            {status.data.identity.endpoint_label || "Not configured"}
          </p>
          <p>
            Profile: {status.data.identity.config_id || "Not configured"} · version {" "}
            {status.data.identity.config_version || "—"} · policy {" "}
            {status.data.configuration.policy_id || "Not configured"} (v
            {status.data.configuration.policy_version || "—"})
          </p>
          <p>
            Transport availability: {label(status.data.availability)}. Diagnosis
            quality: {label(status.data.quality)}
            {status.data.qualification_cohort &&
              ` · cohort ${status.data.qualification_cohort}`}
          </p>
          <p>
            Probe marker: {status.data.last_check?.receipt?.schema_success === true
              ? "Valid harmless probe marker"
              : status.data.last_check?.receipt?.schema_success === false
                ? "Probe marker validation failed"
                : "Not established"}
            {status.data.last_check?.created_at &&
              ` · observed ${time(status.data.last_check.created_at)}`}
          </p>
          <p>
            Actual RCA contract: {status.data.validated_rca
              ? `${label(status.data.validated_rca.status)} · validated ${time(status.data.validated_rca.observed_at)}`
              : "No matching validated operational RCA is recorded"}
          </p>
          <p>Latest matching call usage: {usageText(status.data)}</p>
          <p>
            Latest provider call: {status.data.latest_call
              ? `${label(status.data.latest_call.kind)} · ${label(status.data.latest_call.source)} · ${time(status.data.latest_call.observed_at)} · HTTP ${status.data.latest_call.http_status ?? "not recorded"}`
              : "No matching provider call is recorded"}
          </p>
          <p>
            Dispatch: {status.data.dispatch_allowed
              ? "Allowed"
              : `Paused · ${label(status.data.dispatch_reason)}`}
          </p>
          <p>
            Last check attempt: {time(status.data.last_attempt_at)}
            <br />
            Last check transport success: {time(status.data.last_success_at)}
            <br />
            Latest check outcome: {label(status.data.last_error)}
          </p>
          <p className="muted">
            {nextStep(status.data.last_error || status.data.latest_call?.kind)}
          </p>
          <section aria-label="Configured model limits">
            <h3>Deployment configuration guidance</h3>
            <p>
              API root: {status.data.configuration.api_root || "Not configured"}
              {" · "}Credential reference: {status.data.configuration.credential_reference || "Not configured"}
            </p>
            <p>
              Profile timeout: {status.data.configuration.timeout_seconds ?? "Unavailable"} seconds ·
              output limit: {status.data.configuration.output_limit_parameter || "Not configured"}
              {status.data.configuration.output_limit !== null &&
                ` ${status.data.configuration.output_limit}`}
            </p>
            <p>
              Input/context cap: {status.data.configuration.max_input_bytes ?? "Unavailable"} bytes ·
              policy request cap: {status.data.configuration.max_request_bytes ?? "Unavailable"} bytes ·
              target cap: {status.data.configuration.max_target_bytes ?? "Unavailable"} bytes ·
              response cap: {status.data.configuration.max_response_bytes ?? "Unavailable"} bytes
            </p>
            <p className="muted">
              Configure the OpenAI-compatible API root, including its version prefix,
              and a deployment-side credential reference. Update the trusted profile
              and matching egress policy in deployment configuration; secrets and
              private configuration files are never shown here. Configured limits are
              separate from observed provider usage. Provider quota and remaining
              balance are unavailable unless the provider reports them.
            </p>
          </section>
          {canMutate ? (
            <Button
              disabled={
                check.isPending ||
                ["not_configured", "configuration_invalid", "disabled"].includes(
                  status.data.availability,
                )
              }
              onClick={() => check.mutate()}
            >
              {check.isPending ? "Queuing check…" : "Check configured model"}
            </Button>
          ) : (
            <p>Viewer access: inspect stored checks. An Operator must initiate a new check.</p>
          )}
          <p className="muted">
            A harmless probe checks transport and its fixed response marker only;
            it never establishes RCA validity or quality qualification. Opening,
            refreshing, polling and signing in do not send a provider request.
          </p>
        </>
      )}
      {check.isError && <Failure error={check.error} />}
      {checks.isError ? (
        <Failure error={checks.error} retry={() => void checks.refetch()} />
      ) : checks.data?.items.length ? (
        <section aria-label="Stored model checks">
          <h3>Recent stored checks</h3>
          <ul className="foundation-stack">
            {checks.data.items.map((item) => {
              const receipt = item.receipt;
              return (
                <li key={item.id}>
                  <p>Check {item.id} · {label(item.state)}</p>
                  <p>
                    Transport: {receipt?.transport_success === true
                      ? "Succeeded"
                      : receipt?.transport_success === false
                        ? "Failed"
                        : "Not recorded"} · Probe marker: {receipt?.schema_success === true
                      ? "Valid"
                      : receipt?.schema_success === false
                        ? "Invalid"
                        : "Not recorded"} · Usage: {checkUsage(item)}
                  </p>
                  <p className="muted">
                    Evidence time: {time(
                      typeof receipt?.observed_at === "string"
                        ? receipt.observed_at
                        : item.created_at,
                    )}.
                    This probe does not qualify diagnosis or prove a validated RCA.
                  </p>
                </li>
              );
            })}
          </ul>
        </section>
      ) : (
        <p>No model checks recorded.</p>
      )}
      <p className="muted">
        Showing up to ten recent checks. Missing usage is unavailable, observed zero
        remains zero, and no remaining provider quota is inferred.
      </p>
    </article>
  );
}
