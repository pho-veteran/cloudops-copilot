import type { components } from "@/lib/api/generated/schema";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Report = components["schemas"]["ReportView"];
type Advice = components["schemas"]["ResourceAdvice"];
type Coverage = NonNullable<Advice["coverage"]>;
type Value = Advice["current_request"];

export const words = (value: string) => value.replaceAll("_", " ");
export const time = (value?: string | null) => value ? new Date(value).toISOString().replace("T", " ").replace(".000Z", " UTC").replace(/Z$/, " UTC") : "Not recorded";

function Amount({ value }: { value?: Value | null }) {
  if (!value || value.kind === "unavailable") return <>Unavailable{value?.kind === "unavailable" && ` · ${words(value.reason)}`}</>;
  if (value.kind === "unset") return <>Unset</>;
  if (value.unit === "cores") return <>{value.value} cores</>;
  return <>{(Number(value.value) / 1024 ** 2).toLocaleString(undefined, { maximumFractionDigits: 2 })} MiB <span className="muted">({value.value} bytes)</span></>;
}

function CoverageView({ value }: { value?: Coverage | null }) {
  if (!value) return <p>Coverage unavailable.</p>;
  return <div className="foundation-stack">
    <p>{(value.ratio * 100).toFixed(1)}% · {value.covered_bins}/{value.expected_bins} distinct one-minute bins · longest gap {value.longest_gap_seconds.toFixed(1)} seconds</p>
    <p className="muted">First raw sample: {time(value.first_sample_at)}<br />Latest raw sample: {time(value.last_sample_at)}</p>
    {!!value.reasons?.length && <p>{value.reasons?.map(words).join(" · ")}</p>}
  </div>;
}

export function ReportDetails({ report, historyHref }: { report: Report; historyHref: string }) {
  const result = report.result_v2;
  const identity = report.input_identity ?? result?.identity;
  const profile = report.profile ?? result?.profile;
  return <div className="foundation-stack">
    <div>
      <h2>Selected report</h2>
      <p className="break-all">{report.id}</p>
      <p>Created {time(report.created_at)}</p>
      {!identity && <p className="break-all">Workload record ID: {report.workload_id}</p>}
      <div className="foundation-actions">
        <Badge variant="outline">Job: {words(report.job_phase)}</Badge>
        <Badge variant="outline">Stored outcome: {report.schema_version === 1 ? "Legacy" : report.result_state ? words(report.result_state) : "Pending"}</Badge>
        <Badge variant="outline">Applicability: {report.applicability.state}</Badge>
      </div>
      <p>{report.reason}</p>
      {!!report.applicability.reasons.length && <p>Current applicability: {report.applicability.reasons.map(words).join(" · ")}. This does not change the stored outcome or export.</p>}
      {report.dispatch_paused && <p role="status">Dispatch paused: {report.dispatch_reason ? words(report.dispatch_reason) : "Trusted dispatch authority has paused report work."}</p>}
    </div>
    {identity && <section className="foundation-panel" aria-label="Saved workload identity">
      <h3>{identity.namespace}/{identity.name}</h3>
      <p>Cluster: {identity.cluster}</p><p className="break-all">Deployment UID: {identity.uid}</p>
      <p className="break-all">Workload ID: {identity.workload_id}</p>
      <p>Containers: {identity.containers?.join(", ") || "Not recorded"}</p>
    </section>}
    <Button variant="outline" asChild><a href={historyHref}>Workload report history</a></Button>
    <section className="foundation-panel" aria-label="Report interval and tool">
      <h3>Interval and tool</h3>
      <p>Requested: {report.requested_hours} hours · {report.tool_version}</p>
      {profile && <>
        <p>Strategy: {profile.strategy} · CPU percentile {profile.cpu_percentile} · memory buffer {profile.memory_buffer_percent}%</p>
        <p>Rate/subquery timeframe: {profile.timeframe_minutes} minutes · required tool points: {profile.points_required}</p>
        <p className="break-all">Image: {profile.image_digest ?? "Not recorded"}</p>
        {profile.upstream_commit && <div>
          <p>Pinned source release: {profile.tool_version}</p>
          <p>Source commit:<br /><span className="break-all">{profile.upstream_commit}</span></p>
          <p>Embedded metadata: {profile.embedded_version ?? "Not recorded"}</p>
        </div>}
      </>}
      {result?.window ? <>
        <p><strong>{result.window.kind === "query_envelope" ? "Actual query envelope" : "Actual source interval"}</strong><br />{time(result.window.start)} → {time(result.window.end)}</p>
        {result.window.kind === "query_envelope" && <p>The envelope conservatively contains the tool queries; their exact boundaries can differ.</p>}
        <p>Raw input, including CPU rate support:<br />{time(result.window.raw_input_start)} → {time(result.window.raw_input_end)}</p>
      </> : <p>No completed source interval is recorded.</p>}
    </section>
    {result ? <>
      {result.resources?.length ? <div className="foundation-grid">
        {result.resources?.map(resource => <section className="foundation-panel foundation-stack" key={`${resource.container}:${resource.resource}`} aria-label={`${resource.container} ${resource.resource} advice`}>
          <h3>{resource.container} · {resource.resource === "cpu" ? "CPU" : "Memory"}</h3>
          <Badge variant="outline">{words(resource.state)}</Badge>
          <dl className="report-values">
            <dt>Current request</dt><dd><Amount value={resource.current_request} /></dd>
            <dt>Current limit</dt><dd><Amount value={resource.current_limit} /></dd>
            <dt>Observed</dt><dd><Amount value={resource.observed} /></dd>
            <dt>Recommended request</dt><dd><Amount value={resource.recommended_request} /></dd>
            <dt>Recommended limit</dt><dd><Amount value={resource.recommended_limit} /></dd>
          </dl>
          <p>Statistic: {resource.statistic_definition ?? (resource.statistic ? words(resource.statistic) : "Not recorded")}</p>
          <p>Buffer: {resource.buffer_percent === null || resource.buffer_percent === undefined ? "Not recorded" : `${resource.buffer_percent}%`} · minimum: <Amount value={resource.minimum} /></p>
          <p>Rounding: {resource.rounding ?? "Not recorded"}</p>
          <CoverageView value={resource.coverage} />
          {!!resource.reasons?.length && <p>Reasons: {resource.reasons?.map(words).join(" · ")}</p>}
        </section>)}
      </div> : <p>No numeric resource advice was published. {words(result.reason)}.</p>}
      {!!result.source_evidence?.pods.length && <details className="foundation-panel">
        <summary>Per-Pod coverage and lifetime evidence</summary>
        <div className="foundation-stack">
          {result.source_evidence.pods.map(pod => <section className="foundation-stack border-t pt-4" key={`${pod.uid}:${pod.container}`}>
            <h3>{pod.pod} · {pod.container}</h3>
            <p className="break-all">Pod UID: {pod.uid}<br />ReplicaSet UID: {pod.replicaset_uid}</p>
            <p>Lifetime: {pod.lifetime} · {time(pod.start)} → {time(pod.end)}</p>
            <h4>CPU</h4><CoverageView value={pod.cpu} />
            <h4>Memory</h4><CoverageView value={pod.memory} />
            <h4>Restart evidence</h4><CoverageView value={pod.restarts} />
            {!!pod.reasons?.length && <p>{pod.reasons?.map(words).join(" · ")}</p>}
            {!!pod.memory_reasons?.length && <p>Memory: {pod.memory_reasons?.map(words).join(" · ")}</p>}
          </section>)}
        </div>
      </details>}
      {!!result.limitations?.length && <section aria-label="Report limitations"><h3>Limitations</h3><ul>{result.limitations?.map((text, index) => <li key={index}>{text}</li>)}</ul></section>}
    </> : report.schema_version === 1 ? <section className="foundation-panel">
      <h3>Legacy report</h3><p>Preserved historical output. It is not qualified versioned resource advice.</p>
      {report.migration_disposition && <p>{words(report.migration_disposition)}</p>}
      <details><summary>Preserved legacy data</summary><pre>{JSON.stringify(report.result, null, 2)}</pre></details>
    </section> : <p role="status">The stored result will appear after the report finishes.</p>}
    {!!report.attempts.length && <details className="foundation-panel"><summary>Job attempts ({report.attempts.length})</summary>
      {report.attempts.map(attempt => <p key={attempt.number}>Attempt {attempt.number}: {words(attempt.state)} · {time(attempt.started_at)} → {time(attempt.finished_at)}{attempt.error && ` · ${attempt.error}`}</p>)}
    </details>}
  </div>;
}
