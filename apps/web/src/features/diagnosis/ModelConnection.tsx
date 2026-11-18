import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestKey, settleKey } from "@/features/investigations/request";
import { label } from "./Analysis";

type ModelStatus = components["schemas"]["ModelStatus"];
type Probe = components["schemas"]["ProbeReceiptView"];

const guidance: Record<string, string> = {
  auth_denied:
    "Check the provider credential and its permissions in the worker deployment.",
  rate_limited:
    "Wait for the provider limit to clear before starting another check.",
  timeout:
    "Check provider availability and the configured timeout. The call may have reached the provider.",
  transport_error:
    "Check the configured API root, TLS trust and worker network access.",
  provider_outcome_unknown:
    "Inspect provider availability. An earlier request may still have consumed quota.",
  invalid_envelope:
    "Check that the provider returns a supported Chat Completions response.",
  configuration_changed:
    "Inspect the current profile and egress policy before checking again.",
  credential_unavailable:
    "Provide the configured credential reference to the worker.",
  credential_invalid: "Check the worker credential reference and value.",
  not_configured:
    "Configure a model profile and an egress policy in the deployment.",
  configuration_invalid:
    "Correct the model profile, egress policy or qualification file.",
  destination_mismatch:
    "Match the configured destination to the trusted egress policy.",
};

function timestamp(value: string | null | undefined) {
  return value ? (
    <time dateTime={value}>{new Date(value).toLocaleString()}</time>
  ) : (
    "Not recorded"
  );
}

function probeMarker(receipt: Probe | null | undefined) {
  return receipt?.schema_success === true
    ? "Valid marker"
    : receipt?.http_status != null &&
        receipt.http_status >= 200 &&
        receipt.http_status < 300
      ? "Not validated"
      : "Not established";
}

function ModelEvidence({ status }: { status: ModelStatus }) {
  const {
    identity,
    last_attempt: attempt,
    last_check: check,
    rca_validation: rca,
    limits,
  } = status;
  const receipt = check?.receipt;
  const age =
    receipt?.observed_at &&
    Date.parse(status.assessed_at) - Date.parse(receipt.observed_at);
  const historical =
    typeof age === "number" && age >= status.stale_after_seconds * 1000;
  const reason =
    receipt?.kind && guidance[receipt.kind] && !check?.admitted_at
      ? receipt.kind
      : (status.last_error ?? check?.state ?? status.availability);
  const next = guidance[reason] ?? guidance[status.availability];
  return (
    <>
      <Badge variant="outline">{label(status.availability)}</Badge>
      <p>
        Model {identity.model || "Not configured"} · Endpoint{" "}
        {identity.endpoint_label || "Not configured"}
      </p>
      <dl className="foundation-metadata">
        <dt>Latest provider attempt</dt>
        <dd>
          {attempt ? (
            <>
              {label(attempt.source)} · {timestamp(attempt.admitted_at)}
            </>
          ) : (
            "Not recorded"
          )}
        </dd>
        <dt>Transport</dt>
        <dd>
          {attempt ? label(attempt.transport) : "Not observed"}
          {attempt?.http_status != null && ` · HTTP ${attempt.http_status}`}
          {attempt?.stale && " · Historical"} ·{" "}
          {timestamp(attempt?.observed_at)}
        </dd>
        <dt>Last successful HTTP response</dt>
        <dd>{timestamp(status.last_success_at)}</dd>
        <dt>Last attempt outcome</dt>
        <dd>{label(attempt?.outcome)}</dd>
        <dt>Latest configured probe</dt>
        <dd>
          {check ? (
            <>
              {label(check.state)} ·{" "}
              {timestamp(receipt?.observed_at ?? check.created_at)}
              {historical && " · Historical"}
            </>
          ) : (
            "Not recorded"
          )}
        </dd>
        <dt>Probe marker</dt>
        <dd>{probeMarker(receipt)}</dd>
        <dt>RCA validation</dt>
        <dd>
          {rca ? (
            <>
              {label(rca.outcome)} · {label(rca.purpose)} ·{" "}
              {timestamp(rca.observed_at)}
              {rca.stale && " · Historical"}
            </>
          ) : (
            "Not established"
          )}
        </dd>
        <dt>Quality qualification</dt>
        <dd>
          {label(status.quality)}
          {status.quality === "qualified" &&
            status.qualification_cohort &&
            ` · ${status.qualification_cohort}`}
        </dd>
        <dt>Analysis dispatch</dt>
        <dd>
          {status.dispatch_allowed
            ? "Allowed"
            : `Paused · ${label(status.dispatch_reason)}`}
        </dd>
        <dt>Probe dispatch</dt>
        <dd>
          {status.probe_dispatch_allowed
            ? "Allowed"
            : `Paused · ${label(status.probe_dispatch_reason)}`}
        </dd>
        <dt>Observed usage for latest attempt</dt>
        <dd>
          {attempt?.usage ? (
            <>
              Input {attempt.usage.prompt_tokens ?? "Unavailable"} · Output{" "}
              {attempt.usage.completion_tokens ?? "Unavailable"} · Total{" "}
              {attempt.usage.total_tokens ?? "Unavailable"} tokens ·{" "}
              {timestamp(attempt.observed_at)}
              {attempt.stale && " · Historical"}
            </>
          ) : (
            "Unavailable"
          )}
        </dd>
        <dt>Remaining provider quota</dt>
        <dd>Unavailable</dd>
      </dl>
      {next && <p>{next}</p>}
      {probeMarker(receipt) === "Not validated" && (
        <p>
          Check that the configured model returns the expected marker in a
          supported response.
        </p>
      )}
      <p className="muted">
        A connection probe checks a fixed marker. The probe does not establish
        diagnostic quality.
      </p>
      <details>
        <summary>Configuration and setup</summary>
        <dl className="foundation-metadata">
          <dt>Profile</dt>
          <dd>
            {identity.config_id ?? "Unavailable"} ·{" "}
            {identity.config_version ?? "Unavailable"}
          </dd>
          <dt>Behavior identity</dt>
          <dd className="break-all">
            {identity.behavior_digest ?? "Unavailable"}
          </dd>
          <dt>Egress policy identity</dt>
          <dd className="break-all">
            {identity.policy_digest ?? "Unavailable"}
          </dd>
          <dt>Validator</dt>
          <dd>{identity.validator_version ?? "Unavailable"}</dd>
          {limits && (
            <>
              <dt>Request deadline</dt>
              <dd>{limits.request_deadline_seconds}s</dd>
              <dt>Request and context caps</dt>
              <dd>
                {limits.max_request_bytes} request bytes ·{" "}
                {limits.max_context_bytes} context target bytes
              </dd>
              <dt>Output budget</dt>
              <dd>
                {limits.max_output_tokens != null
                  ? `${limits.max_output_tokens} tokens (${limits.output_limit_param})`
                  : limits.output_limit_param === "omit"
                    ? "Enforced by the endpoint policy"
                    : "Unavailable"}
              </dd>
              <dt>Call budgets</dt>
              <dd>
                {limits.max_analysis_calls} per analysis ·{" "}
                {limits.max_probe_calls} per probe · {status.cooldown_seconds}s
                probe cooldown
              </dd>
            </>
          )}
        </dl>
        <p>
          Set the Chat Completions API root with its version prefix in the
          deployment profile. The client appends /chat/completions. Set the
          model ID, egress policy, deadline and budget limits there. Provide the
          credential through the worker's configured environment reference.
        </p>
        <p>
          Knowledge currently uses full-text retrieval. Embedding availability
          is separate from this model connection.
        </p>
      </details>
    </>
  );
}

export function ModelConnection({ canMutate }: { canMutate: boolean }) {
  const cache = useQueryClient();
  const status = useQuery({
    queryKey: ["model-status"],
    queryFn: async () => unwrap(await api.GET("/api/diagnosis/status")),
    refetchInterval: 10000,
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
    refetchInterval: 10000,
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
  const latest = status.data?.last_check;
  const assessed = status.data ? Date.parse(status.data.assessed_at) : 0;
  const active =
    latest &&
    ["queued", "in_flight"].includes(latest.state) &&
    Date.parse(latest.deadline) > assessed;
  const cooling =
    latest &&
    assessed - Date.parse(latest.created_at) <
      (status.data?.cooldown_seconds ?? 0) * 1000;
  const allowed =
    status.data?.probe_dispatch_allowed &&
    ["available", "evaluation_only", "unqualified"].includes(
      status.data.availability,
    );

  return (
    <article
      className="foundation-panel foundation-stack min-w-0 break-words"
      aria-label="Model connection"
    >
      <h2>Model connection</h2>
      {status.isPending && <Loading />}
      {status.isError && (
        <Failure error={status.error} retry={() => void status.refetch()} />
      )}
      {status.data && <ModelEvidence status={status.data} />}
      {canMutate ? (
        <Button
          disabled={
            check.isPending ||
            status.isError ||
            !allowed ||
            !!active ||
            !!cooling
          }
          onClick={() => check.mutate()}
        >
          {check.isPending
            ? "Queuing check…"
            : active
              ? "Check pending"
              : "Check configured model"}
        </Button>
      ) : (
        <p>Viewer access includes stored model checks.</p>
      )}
      {canMutate && !active && cooling && (
        <p>Wait for the probe cooldown before checking again.</p>
      )}
      {check.isError && <Failure error={check.error} />}
      {checks.isError && (
        <Failure error={checks.error} retry={() => void checks.refetch()} />
      )}
      {checks.isPending && <Loading />}
      {checks.data && (
        <>
          <h3>Recent model checks</h3>
          {checks.data.items.length ? (
            <ul className="foundation-stack">
              {checks.data.items.map((item) => {
                const receipt = item.receipt;
                const previousConfig =
                  status.data?.identity.behavior_digest &&
                  (item.identity?.behavior_digest !==
                    status.data.identity.behavior_digest ||
                    item.identity?.policy_digest !==
                      status.data.identity.policy_digest);
                return (
                  <li key={item.id}>
                    <p>
                      {item.identity?.endpoint_label || "Unknown endpoint"} ·{" "}
                      {item.identity?.model || "Unknown model"} ·{" "}
                      {item.identity?.config_version || "Unknown version"} ·{" "}
                      {label(item.state)}
                      {previousConfig && " · Previous configuration"}
                    </p>
                    <p>
                      Transport:{" "}
                      {receipt?.transport_success === true
                        ? "Succeeded"
                        : receipt?.http_status != null
                          ? "Rejected"
                          : "Not observed"}{" "}
                      · Probe marker: {probeMarker(receipt)} · Observed total
                      tokens: {receipt?.usage?.total_tokens ?? "Unavailable"}
                    </p>
                    <p className="muted">
                      Created {timestamp(item.created_at)} · Observed{" "}
                      {timestamp(receipt?.observed_at)}
                      {receipt?.kind && ` · ${label(receipt.kind)}`}
                    </p>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p>No model checks recorded.</p>
          )}
        </>
      )}
    </article>
  );
}
