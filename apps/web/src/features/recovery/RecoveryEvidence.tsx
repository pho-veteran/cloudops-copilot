import { useQuery } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap, ApiError } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { buildIncidentHash } from "@/app/routes";

export const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const text = (value: unknown): string => value == null ? "Not recorded" :
  typeof value === "object" ? JSON.stringify(value) : String(value);
export const words = (value: unknown) => text(value).replaceAll("_", " ");
export const uuidValue = (value: unknown): value is string =>
  typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

export function ExecutionOutcome({ state, outcome }: { state: string; outcome: components["schemas"]["Outcome"] }) {
  if (!outcome) return <p>Outcome provenance is unavailable for this historical record. Inspect retained evidence and current target authority before a new change.</p>;
  return <div className="foundation-stack min-w-0 break-words" aria-label="Execution outcome">
    <h4>{words(state)}</h4>
    <dl className="foundation-metadata">
      <dt>Write attribution</dt><dd>{words(outcome.attribution)}</dd>
      <dt>Application verification</dt><dd>{words(outcome.verification)}</dd>
      <dt>Future write authority</dt><dd>{words(outcome.future_authority)}</dd>
    </dl>
    <p>{outcome.reason}</p>
    {outcome.attribution === "outcome_unknown" && <p>Write outcome unknown. Matching current fields do not establish a write. Keep the target quarantine and use trusted deployment maintenance for future authority.</p>}
    {outcome.attribution === "acknowledged" && outcome.verification === "inconclusive" && <p>The write was acknowledged; application recovery is inconclusive. Inspect fresh evidence or hand off this result.</p>}
    {state === "no_op" && <p>No functional change. This is excluded from remediation success.</p>}
    {state === "rolled_back" && <p>The separately approved inverse passed its post-checks. Its original forward result remains unchanged.</p>}
    {outcome.reconciliation && <p>Receipt reads: {outcome.reconciliation.attempts} / {outcome.reconciliation.maximum_attempts} · fixed deadline {outcome.reconciliation.deadline ?? "Unavailable"}. No mutation is retried.</p>}
  </div>;
}

export function useRecoveryAvailability(workloadId?: string) {
  return useQuery({ queryKey: ["recovery-availability", workloadId], enabled: !!workloadId,
    queryFn: async () => {
      const value = unwrap(await api.GET("/api/workloads/{workload_id}/recovery-availability", { params: { path: { workload_id: workloadId! } } }));
      if (value.workload_id !== workloadId || typeof value.uid !== "string" || !["enabled", "quarantined", "unavailable"].includes(value.target_authority) || !["ready_for_preflight", "blocked", "unavailable"].includes(value.application_state) || !Array.isArray(value.qualified_templates) || !Array.isArray(value.blocking_executions))
        throw new ApiError(502, "recovery_availability_incomplete", "Current target authority is incomplete. Refresh before preparing a change.");
      return value;
    },
    retry: false, refetchInterval: 10000, refetchIntervalInBackground: false,
  });
}

export function AuthoritySummary({ value }: { value: components["schemas"]["RecoveryAvailability"] }) {
  return <div className="foundation-stack min-w-0 break-words">
    <p>Target authority: <strong>{words(value.target_authority)}</strong> · application: {words(value.application_state)}</p>
    <p className="mono break-all">Original UID: {value.uid}<br />Epoch: {value.epoch ?? "Unavailable"}</p>
    <p>{value.reason}</p><p className="muted">Observed {value.observed_at}. This read does not authorize a change.</p>
    <p>Qualified verification templates: {value.qualified_templates.length ? value.qualified_templates.map(words).join(", ") : "None configured; qualified recovery cannot be claimed."}</p>
    {value.blocking_executions.map(row => <article className="foundation-panel" key={row.id}>
      <a className="break-all" href={buildIncidentHash(row.incident_id, "", "#/queue", "", "", { execution: row.id })}>Inspect blocking operation {row.id}</a>
      <ExecutionOutcome state={row.state} outcome={row.outcome} />
    </article>)}
    {value.blocking_executions_truncated && <p>Additional blockers exist. Inspect target history before planning.</p>}
  </div>;
}

export function RecoveryAuthority({ workloadId }: { workloadId: string }) {
  const query = useRecoveryAvailability(workloadId);
  return <section className="foundation-panel foundation-stack min-w-0" aria-label="Recovery target authority">
    <h3>Recovery target authority</h3>
    {query.isPending ? <Loading /> : query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : <AuthoritySummary value={query.data} />}
  </section>;
}

export function VerificationDetails({ value }: { value: components["schemas"]["VerificationView"] }) {
  const profile = record(value.profile), result = record(value.result), app = record(result.application);
  return <section className="foundation-stack min-w-0" aria-label="Fresh verification evidence">
    <h4>Fresh verification evidence</h4>
    <p>Profile {text(profile.id)} · {text(profile.version)}</p>
    <p className="mono break-all">Profile digest: {value.profile_digest}</p>
    <dl className="foundation-metadata">
      <dt>ACK anchor</dt><dd>{value.acknowledged_at ?? "No acknowledged window"}</dd>
      <dt>Fixed deadline</dt><dd>{value.deadline ?? "Not established"}</dd>
      <dt>Reserved slots</dt><dd>{value.next_slot}; stored observations {Object.keys(value.samples).length}</dd>
      <dt>First finalized</dt><dd>{value.finalized_at ?? "Not finalized"}</dd>
      <dt>Qualified rollout</dt><dd>{text(result.valid_rollout_samples)} / {text(result.expected_rollout_samples)}</dd>
      <dt>Application samples</dt><dd>{text(app.valid_samples)} / {text(app.expected_samples)} · {words(app.state)}</dd>
      <dt>Observed offered RPS</dt><dd>{text(app.offered_rps)}</dd>
    </dl>
    <p>{text(result.interpretation)}</p>
    {Array.isArray(result.reason_codes) && !!result.reason_codes.length && <p>Evidence gaps: {result.reason_codes.map(words).join(", ")}</p>}
    {Object.entries(value.samples).map(([slot, sample]) => {
      const rollout = record(record(sample).rollout);
      return <details key={slot}><summary>Observation slot {slot} · {words(rollout.health)} · {words(rollout.coverage)}</summary><p>Observed: {text(rollout.observed_at)}</p><pre className="max-h-80 overflow-auto">{JSON.stringify(sample, null, 2)}</pre></details>;
    })}
    <p className="muted">Reserved slots are not passing samples. Missing, stale or inadequate traffic is never zero-filled. Acknowledgement alone is not causal proof; memory success is an observed mitigation, not a repaired leak.</p>
  </section>;
}
