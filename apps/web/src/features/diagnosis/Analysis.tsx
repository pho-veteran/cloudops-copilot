import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap } from "@/lib/api/client";
import {
  buildIncidentHash,
  buildKnowledgeHash,
  incidentReturnHash,
  safeRoute,
} from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestKey, settleKey } from "@/features/investigations/request";
import { runKeys } from "@/features/investigations/queries";

export type Analysis = components["schemas"]["AnalysisView"];
type Evidence = components["schemas"]["ResolvedEvidence"];
type Result = components["schemas"]["RCAResult"];
export const analysisKey = (incident: string, run: string) =>
  ["analysis", incident, run] as const;
export const label = (value: string | null | undefined) =>
  value?.replaceAll("_", " ") || "Not recorded";

function TextList({ title, items }: { title: string; items: string[] }) {
  return (
    <section>
      <h4>{title}</h4>
      {items.length ? (
        <ul>
          {items.map((text, i) => (
            <li className="whitespace-pre-wrap" key={i}>
              {text}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">None recorded.</p>
      )}
    </section>
  );
}

function EvidenceLinks({
  items,
  incident,
}: {
  items: Evidence[];
  incident: string;
}) {
  const route = safeRoute(window.location.hash);
  return items.length ? (
    <ul className="foundation-stack">
      {items.map((item) => (
        <li key={item.alias}>
          <a
            href={buildIncidentHash(
              incident,
              item.citation.run_id,
              incidentReturnHash(window.location.hash),
              route.id === incident ? route.resolution : "",
              item.citation.evidence_id,
              {
                ...route.selection,
                locatorKind: item.citation.locator?.kind,
                locatorKey: item.citation.locator?.key,
              },
            )}
          >
            {item.alias} · {item.source_key} · exact evidence
          </a>
          <p className="muted">
            {label(item.coverage_status)} · {label(item.time_relation)}
          </p>
        </li>
      ))}
    </ul>
  ) : (
    <p className="muted">No telemetry references recorded.</p>
  );
}

export function StoredFindings({ result }: { result: Result }) {
  return (
    <div className="foundation-stack">
      <Badge variant="outline">
        {label(result.status)} · {label(result.cause_category)}
      </Badge>
      <p className="whitespace-pre-wrap">{result.summary}</p>
      <p className="muted">
        Stored advice for this Run. Structural validation does not establish
        that every claim is correct; recovery requires fresh server checks and
        explicit approval.
      </p>
      <section>
        <h4>Supporting evidence</h4>
        <EvidenceLinks
          items={result.supporting}
          incident={result.incident_id}
        />
      </section>
      <section>
        <h4>Contradicting evidence</h4>
        <EvidenceLinks
          items={result.contradicting}
          incident={result.incident_id}
        />
      </section>
      <section>
        <h4>Alternatives</h4>
        {result.alternatives.length ? (
          result.alternatives.map((item, i) => (
            <div key={i}>
              <p className="whitespace-pre-wrap">
                {item.hypothesis}: {item.reason}
              </p>
              <EvidenceLinks
                items={item.evidence}
                incident={result.incident_id}
              />
            </div>
          ))
        ) : (
          <p className="muted">None recorded.</p>
        )}
      </section>
      <section>
        <h4>Ruled-out hypotheses</h4>
        {result.ruled_out.length ? (
          result.ruled_out.map((item, i) => (
            <div key={i}>
              <p className="whitespace-pre-wrap">
                {label(item.family)}: {item.reason}
              </p>
              <EvidenceLinks
                items={item.evidence}
                incident={result.incident_id}
              />
            </div>
          ))
        ) : (
          <p className="muted">No hypothesis marked ruled out.</p>
        )}
      </section>
      <TextList
        title="Unverified checks"
        items={result.unverified_checks.map(
          (item) => `${item.check}: ${item.reason}`,
        )}
      />
      <TextList title="Missing evidence" items={result.missing_evidence} />
      <TextList title="Limitations" items={result.limitations} />
      <TextList title="Next steps" items={result.next_steps} />
      {result.candidate_template_id && (
        <p>
          Advisory template:{" "}
          <strong>{label(result.candidate_template_id)}</strong>. This is not an
          executable plan.
        </p>
      )}
      <section>
        <h4>Runbook methodology</h4>
        {result.runbook_citations.length ? (
          <ul>
            {result.runbook_citations.map((item) => (
              <li key={item.alias}>
                <a
                  href={buildKnowledgeHash({
                    document: item.reference.document_version_id,
                    chunk: item.reference.chunk_id,
                    incident: result.incident_id,
                    run: result.run_id,
                    selection: item.selection_id,
                    returnTo: window.location.hash,
                  })}
                >
                  {item.alias} · {item.reference.document_key} · version{" "}
                  {item.reference.version}
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            No runbook was cited. Guidance is separate from factual telemetry.
          </p>
        )}
      </section>
    </div>
  );
}

export function AnalysisSummary({ analysis }: { analysis: Analysis }) {
  return (
    <div className="foundation-stack min-w-0 break-words">
      <p role="status">
        Analysis: {label(analysis.status)} · {label(analysis.reason)}
      </p>
      {analysis.model_slot_busy && (
        <p>
          Waiting for model capacity. No request has been admitted for this
          attempt.
        </p>
      )}
      {analysis.result ? (
        <StoredFindings result={analysis.result} />
      ) : (
        <p>
          No validated result is stored for this Run. Evidence and saved manual
          guidance remain available.
        </p>
      )}
      <details>
        <summary>Analysis provenance and history</summary>
        <dl className="foundation-metadata">
          <dt>Model</dt>
          <dd>{analysis.identity?.model || "Unavailable"}</dd>
          <dt>Endpoint label</dt>
          <dd>{analysis.identity?.endpoint_label || "Unavailable"}</dd>
          <dt>Calls / attempts / repairs</dt>
          <dd>
            {analysis.calls ?? 0}/{analysis.max_calls ?? 3} calls ·{" "}
            {analysis.attempts ?? 0}/{analysis.max_attempts ?? 3} attempts ·{" "}
            {analysis.repairs ?? 0}/{analysis.max_repairs ?? 1} repairs
          </dd>
          <dt>Original deadline</dt>
          <dd>{analysis.deadline || "Not recorded"}</dd>
        </dl>
        <p className="muted">
          Usage is observed, not remaining quota. Missing usage is unavailable.
        </p>
        <ul>
          {(analysis.history ?? []).map((item, i) => {
            const receipt =
              item.receipt && typeof item.receipt === "object"
                ? (item.receipt as Record<string, unknown>)
                : {};
            const usage =
              receipt.usage && typeof receipt.usage === "object"
                ? (receipt.usage as Record<string, unknown>)
                : {};
            return (
              <li key={i}>
                Call {String(item.number ?? i + 1)} ·{" "}
                {String(receipt.kind ?? "Outcome unavailable")} · total tokens:{" "}
                {typeof usage.total_tokens === "number"
                  ? usage.total_tokens
                  : "Unavailable"}
              </li>
            );
          })}
        </ul>
        <pre className="max-h-80 max-w-full overflow-auto whitespace-pre-wrap break-all">
          {JSON.stringify(
            {
              identity: analysis.identity,
              context: analysis.context,
              attempts: analysis.attempt_history,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </div>
  );
}

export function AnalysisPanel({
  incidentId,
  runId,
  canMutate,
  onRecovery,
}: {
  incidentId: string;
  runId: string;
  canMutate: boolean;
  onRecovery?: () => void;
}) {
  const cache = useQueryClient();
  const key = analysisKey(incidentId, runId);
  const query = useQuery({
    queryKey: key,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs/{run_id}/analysis", {
          params: { path: { incident_id: incidentId, run_id: runId } },
        }),
      ),
    refetchInterval: 2000,
    retry: false,
  });
  const scope = `analysis-resume:${incidentId}:${runId}`;
  const resume = useMutation({
    retry: false,
    mutationFn: async () =>
      unwrap(
        await api.POST(
          "/api/incidents/{incident_id}/runs/{run_id}/analysis/resume",
          {
            params: {
              path: { incident_id: incidentId, run_id: runId },
              header: { "idempotency-key": requestKey(scope, {}) },
            },
            body: {},
          },
        ),
      ),
    onSuccess: (data) => {
      cache.setQueryData(key, data);
      void cache.invalidateQueries({ queryKey: runKeys.all(incidentId) });
    },
    onSettled: (_data, error) => {
      settleKey(scope, error ?? undefined);
      void cache.invalidateQueries({ queryKey: key });
    },
  });
  return (
    <article
      className="foundation-panel foundation-stack min-w-0 break-words"
      aria-label="Stored analysis findings"
    >
      <h2>Findings</h2>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <AnalysisSummary analysis={query.data} />
          {onRecovery && <Button variant="outline" onClick={onRecovery}>{canMutate ? "Inspect this Run's recovery applicability" : "Inspect recovery history"}</Button>}
          {query.data.status === "waiting_for_quota" && (
            <section>
              <h3>Waiting for provider quota</h3>
              <p>
                Earliest retry: {query.data.earliest_retry || "Not provided"}.
                The original deadline and used calls are retained.
              </p>
              {canMutate ? (
                <Button
                  disabled={!query.data.resume_allowed || resume.isPending}
                  onClick={() => resume.mutate()}
                >
                  {resume.isPending ? "Resuming…" : "Resume analysis"}
                </Button>
              ) : (
                <p>
                  Viewer access: an Operator must explicitly resume analysis.
                </p>
              )}
              {!query.data.resume_allowed && (
                <p>{label(query.data.resume_denial)}</p>
              )}
            </section>
          )}
          {query.data.status === "failed" && (
            <p>
              Request a new investigation for fresh evidence. A request with an
              unknown provider outcome is never replayed automatically.
            </p>
          )}
        </>
      )}
      {resume.isError && <Failure error={resume.error} />}
    </article>
  );
}
