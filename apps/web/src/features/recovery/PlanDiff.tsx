import type { components } from "@/lib/api/generated/schema";
import { buildIncidentHash, incidentReturnHash, safeRoute } from "@/app/routes";
import { record, text, words, uuidValue } from "./RecoveryEvidence";

export function PlanDiff({ plan }: { plan: components["schemas"]["PlanView"] }) {
  const content = record(plan.content), before = record(content.before), desired = record(content.desired);
  const rows = [{ field: "replicas", before: before.replicas, desired: desired.replicas ?? content.replicas }];
  const old = Array.isArray(before.containers) ? before.containers.map(record) : [];
  const next = Array.isArray(desired.containers) ? desired.containers.map(record) : [];
  for (const container of old) {
    const target = next.find(item => item.name === container.name) ?? {};
    rows.push({ field: `${text(container.name)} image`, before: container.image, desired: target.image });
    for (const group of ["requests", "limits"]) for (const resource of ["cpu", "memory"])
      rows.push({ field: `${text(container.name)} ${resource} ${group}`, before: record(record(container.resources)[group])[resource], desired: record(record(target.resources)[group])[resource] });
  }
  const provenance = record(content.provenance), rules = record(content.policy);
  return <div className="foundation-stack min-w-0 break-words">
    <h3>Plan version {plan.version} · {words(plan.state)}</h3>
    <p>{words(plan.template)} · {words(plan.origin || "Legacy origin unavailable")}</p>
    <p className="mono break-all">{plan.id} · {plan.digest}</p>
    <p className="mono break-all">Original UID: {text(record(content.workload).uid)}</p>
    <p>Expires {plan.expires_at} · policy {text(rules.id)} / {text(rules.version)}</p>
    {plan.forward_execution_id && <p className="break-all">Captured inverse of forward operation {plan.forward_execution_id}. No alternate values are accepted.</p>}
    {uuidValue(provenance.run_id) && <a href={buildIncidentHash(plan.incident_id, provenance.run_id, incidentReturnHash(window.location.hash), "", "", safeRoute(window.location.hash).selection)}>Inspect exact source Run {provenance.run_id}</a>}
    {plan.source_analysis_id && <p className="mono break-all">Source analysis: {plan.source_analysis_id}</p>}
    <p>Rationale: {plan.rationale}</p>
    <div className="overflow-x-auto"><table><caption>Exact selected-field values</caption><thead><tr><th>Field</th><th>Before</th><th>Desired</th></tr></thead><tbody>{rows.map(row => <tr key={row.field}><th>{row.field}</th><td className="break-all">{text(row.before)}</td><td className="break-all">{text(row.desired)}</td></tr>)}</tbody></table></div>
    {Array.isArray(content.prerequisites) && <ul>{content.prerequisites.map((value, i) => <li key={i}>{text(value)}</li>)}</ul>}
    <p>Fresh target, controller and full candidate capacity checks remain mandatory. Approval does not prove application recovery.</p>
    <details><summary>Recorded plan and provenance</summary><pre className="max-h-80 overflow-auto">{JSON.stringify(plan.content, null, 2)}</pre></details>
  </div>;
}
