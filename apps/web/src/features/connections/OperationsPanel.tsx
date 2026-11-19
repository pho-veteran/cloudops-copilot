import { useQuery } from "@tanstack/react-query";
import { Failure, Loading } from "@/app/Feedback";
import { buildIncidentHash } from "@/app/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";

type JobKindStatus = components["schemas"]["JobKindStatus"];
type BlockedRun = components["schemas"]["BlockedRun"];
type QuotaWaitingRun = components["schemas"]["QuotaWaitingRun"];

const jobNames: Record<JobKindStatus["kind"], string> = {
  development_collection_v1: "Investigation collection",
  diagnosis_analysis_v1: "Diagnosis analysis",
  diagnosis_probe_v1: "Model connection check",
  recommendation_report_v1: "Recommendation report",
  recovery_execution_v1: "Recovery execution",
  recovery_reconciliation_v1: "Recovery reconciliation",
  recovery_verification_v1: "Recovery verification",
};

const dispatchReasons: Record<NonNullable<JobKindStatus["dispatch_reason"]>, string> = {
  permit_missing: "The trusted dispatch permit is missing. Ask the host administrator to check the permit mount.",
  permit_invalid: "The trusted dispatch permit is invalid. Ask the host administrator to check it.",
  permit_stale: "The trusted dispatch permit is stale. Ask the host administrator to check its validity.",
  recovery_mode: "Trusted authority has paused this job kind for host recovery.",
  class_denied: "The trusted dispatch permit does not allow this job kind.",
};

function Time({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

function formatAge(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function JobStatus({ job }: { job: JobKindStatus }) {
  const counts = job.counts;
  return (
    <li className="foundation-receipt foundation-stack">
      <h4>{jobNames[job.kind]}</h4>
      <div>
        <Badge variant="outline">
          {job.dispatch === "paused" ? "Dispatch paused" : "Dispatch allowed"}
        </Badge>
        {job.dispatch_reason && <p>{dispatchReasons[job.dispatch_reason]}</p>}
      </div>
      {counts ? (
        <>
          <p>Queued {counts.queued} · Running {counts.running} · Held {counts.held}</p>
          <p>
            Runnable {counts.runnable} · Due retry {counts.due_retry} · Future retry{" "}
            {counts.future_retry} · Expired lease {counts.expired_lease ?? "Unknown"}
          </p>
          {counts.earliest_future_retry_at && (
            <p>Next scheduled retry <Time value={counts.earliest_future_retry_at} /></p>
          )}
          {counts.verification_slots && (
            <>
              <p>Due verification slots {counts.verification_slots.due} · Future slots {counts.verification_slots.future}</p>
              {counts.verification_slots.earliest_future_at && (
                <p>Next verification slot <Time value={counts.verification_slots.earliest_future_at} /></p>
              )}
            </>
          )}
        </>
      ) : <p>Job counts unknown.</p>}
    </li>
  );
}

function RunLink({ run }: { run: BlockedRun }) {
  return <a href={buildIncidentHash(run.incident_id, run.run_id)}>{run.incident_title}</a>;
}

function QuotaRetry({ run }: { run: QuotaWaitingRun }) {
  return run.earliest_retry_at
    ? <p>Earliest retry <Time value={run.earliest_retry_at} />. Review resume eligibility in the Run.</p>
    : <p>Earliest retry unknown. Review resume eligibility in the Run.</p>;
}

export function OperationsPanel() {
  const query = useQuery({
    queryKey: ["connection-operations"],
    queryFn: async () => unwrap(await api.GET("/api/connections/operations")),
  });
  const data = query.data;
  const refresh = () => void query.refetch();
  return (
    <section className="foundation-panel foundation-stack" aria-labelledby="operations">
      <h2 id="operations">Operations</h2>
      {query.isPending ? <Loading /> : !data ? <Failure error={query.error} retry={refresh} /> : (
        <>
          <p className="muted">Assessed <Time value={data.assessed_at} /></p>
          <div className="foundation-actions">
            <Button variant="outline" disabled={query.isFetching} onClick={refresh}>
              {query.isFetching ? "Refreshing…" : "Refresh operations"}
            </Button>
          </div>
          {query.isError && <Failure error={query.error} retry={refresh} />}
          <section className="foundation-stack" aria-labelledby="operations-jobs">
            <h3 id="operations-jobs">Active jobs</h3>
            <p className="muted">Runnable jobs are queued and due. A dispatch pause can still prevent them from starting.</p>
            {data.jobs_capped && (
              <p>There are more than {data.job_read_limit} active jobs in the supported kinds. Counts and queue age are unknown.</p>
            )}
            <p>
              Oldest runnable job age{" "}
              {data.oldest_runnable_age_seconds !== null
                ? formatAge(data.oldest_runnable_age_seconds)
                : data.jobs_capped ? "Unknown" : "Unavailable (no runnable jobs)"}
            </p>
            <ul className="foundation-grid">
              {data.jobs.map((job) => <JobStatus key={job.kind} job={job} />)}
            </ul>
          </section>
          <section className="foundation-stack" aria-labelledby="operations-capacity">
            <h3 id="operations-capacity">Waiting for collection capacity</h3>
            <p>A worker can admit these Runs when collection capacity and dispatch allow it.</p>
            {data.capacity_deferred.length === 0 && <p>No Runs are waiting for collection capacity.</p>}
            {data.capacity_deferred_has_more && (
              <p>Showing the oldest {data.run_sample_limit} capacity-deferred Runs. More Runs are waiting.</p>
            )}
            {data.capacity_deferred.length > 0 && (
              <ul className="foundation-stack">
                {data.capacity_deferred.map((run) => (
                  <li key={run.run_id} className="foundation-receipt">
                    <RunLink run={run} /> · Requested <Time value={run.created_at} />
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="foundation-stack" aria-labelledby="operations-quota">
            <h3 id="operations-quota">Waiting for diagnosis quota</h3>
            {data.quota_waiting.length === 0 && <p>No Runs are waiting for diagnosis quota.</p>}
            {data.quota_waiting_has_more && (
              <p>Showing the oldest {data.run_sample_limit} quota-waiting Runs. More Runs are waiting.</p>
            )}
            {data.quota_waiting.length > 0 && (
              <ul className="foundation-stack">
                {data.quota_waiting.map((run) => (
                  <li key={run.run_id} className="foundation-receipt">
                    <RunLink run={run} /> · Requested <Time value={run.created_at} />
                    <QuotaRetry run={run} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  );
}
