import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { buildIncidentHash, incidentReturnHash, safeRoute } from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { CitationTarget } from "./CitationTarget";
import { MetricEvidence } from "./MetricEvidence";
import { LogEvidence } from "./LogEvidence";

type Run = components["schemas"]["RunView"];
type Evidence = components["schemas"]["EvidenceView"];

export function coverageLabel(evidence: Evidence) {
  return evidence.schema_version === 1
    ? "Not captured (legacy)"
    : evidence.coverage.status;
}

function EvidenceDetail({
  incidentId,
  runId,
  evidenceId,
  record,
}: {
  incidentId: string;
  runId: string;
  evidenceId: string;
  record: Evidence | undefined;
}) {
  const path = {
    incident_id: incidentId,
    run_id: runId,
    evidence_id: evidenceId,
  };
  const metadata = useQuery({
    queryKey: ["evidence", incidentId, runId, evidenceId],
    queryFn: async () =>
      unwrap(
        await api.GET(
          "/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}",
          { params: { path } },
        ),
      ),
    enabled: !record,
    retry: false,
  });
  // Run polling owns the current pin/availability projection. Only unresolved
  // exact IDs need a separate scoped lookup; do not keep a second stale copy.
  const e = record ?? metadata.data;
  const content = useQuery({
    queryKey: ["evidence-content", incidentId, runId, evidenceId],
    queryFn: async () =>
      unwrap(
        await api.GET(
          "/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/content",
          { params: { path } },
        ),
      ),
    enabled: e?.content_status === "available",
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  if (!e)
    return metadata.isError ? (
      <Failure error={metadata.error} retry={() => void metadata.refetch()} />
    ) : (
      <Loading />
    );
  const contentError = content.error instanceof ApiError ? content.error : null;
  return (
    <>
      <p>
        {coverageLabel(e)} · Content{" "}
        {contentError?.status === 410
          ? "expired"
          : contentError?.status === 409
            ? "unavailable"
            : e.content_status}
      </p>
      {e.source_kind === "fixture" && (
        <p>Synthetic development fixture; not a live source.</p>
      )}
      <dl className="foundation-metadata">
        <dt>Evidence ID</dt>
        <dd className="mono">{e.id}</dd>
        <dt>Source</dt>
        <dd>
          {e.source_key} · {e.source_kind}
        </dd>
        <dt>Workload UID</dt>
        <dd className="mono">{e.workload_uid}</dd>
        <dt>Window</dt>
        <dd>
          {e.requested_window
            ? `${e.requested_window.start} → ${e.requested_window.end}`
            : "Not captured"}
        </dd>
        <dt>Time relation</dt>
        <dd>{e.time_relation}</dd>
        <dt>Observed</dt>
        <dd>
          {e.observed_at
            ? new Date(e.observed_at).toLocaleString()
            : "Not captured"}
        </dd>
        <dt>Collected</dt>
        <dd>{new Date(e.collected_at).toLocaleString()}</dd>
        <dt>Coverage</dt>
        <dd>
          {coverageLabel(e)}
          {e.schema_version !== 1 && e.coverage.reason_codes.length
            ? ` · ${e.coverage.reason_codes.join(", ")}`
            : ""}
        </dd>
        <dt>Versions</dt>
        <dd>
          Catalog {e.catalog_version} · schema {e.schema_version} ·{" "}
          {e.payload_kind} v{e.payload_version} · redaction{" "}
          {e.redaction_policy_version}
        </dd>
        <dt>SHA-256</dt>
        <dd className="mono">{e.sha256 || "Not captured"}</dd>
        <dt>Expires</dt>
        <dd>
          {e.expires_at
            ? new Date(e.expires_at).toLocaleString()
            : "Not scheduled"}
        </dd>
        <dt>Pin</dt>
        <dd>{e.pinned ? "Pinned" : "Not pinned"}</dd>
        {e.query_metadata && (
          <>
            <dt>Query metadata</dt>
            <dd>
              <pre className="evidence-json">
                {JSON.stringify(e.query_metadata, null, 2)}
              </pre>
            </dd>
          </>
        )}
      </dl>
      {e.content_status === "expired" && (
        <p>Evidence content has expired; metadata remains available.</p>
      )}
      {e.content_status === "unavailable" && (
        <p>Evidence content is unavailable.</p>
      )}
      {e.payload_kind === "series" &&
        (e.content_status !== "available" || contentError?.status === 409 || contentError?.status === 410) && (
          <MetricEvidence
            incidentId={incidentId}
            runId={runId}
            evidence={e}
            content={null}
            window={e.requested_window}
          />
        )}
      {e.content_status === "available" &&
        (content.isPending ? (
          <Loading />
        ) : content.isError ? (
          contentError?.status === 410 ? (
            <p>Evidence content has expired; metadata remains available.</p>
          ) : contentError?.status === 409 ? (
            <p>Evidence content is unavailable.</p>
          ) : (
            <Failure
              error={content.error}
              retry={() => void content.refetch()}
            />
          )
        ) : (
          <>
            {content.data.payload_kind === "series" ? (
              <MetricEvidence
                incidentId={incidentId}
                runId={runId}
                evidence={e}
                content={content.data}
                window={e.requested_window}
              />
            ) : (
              <>
                <h3>Stored content</h3>
                {content.data.payload.truncated === true && (
                  <p>
                    Log output was truncated at the collection cap; only a bounded
                    tail is retained.
                  </p>
                )}
                {content.data.payload_kind === "logs" ? (
                  <LogEvidence payload={content.data.payload} />
                ) : Object.keys(content.data.payload).length === 0 ? (
                  <p>Content is empty.</p>
                ) : (
                  <pre className="evidence-json">
                    {JSON.stringify(content.data.payload, null, 2)}
                  </pre>
                )}
              </>
            )}
          </>
        ))}
    </>
  );
}

export function EvidenceCatalog({
  incidentId,
  run,
  selectedEvidence,
  selectedResolution,
}: {
  incidentId: string;
  run: Run;
  selectedEvidence: string;
  selectedResolution: string;
}) {
  const returnTo = incidentReturnHash(window.location.hash);
  const [showTargets, setShowTargets] = useState(false);
  const targetSelected = Boolean(safeRoute(window.location.hash).selection?.locatorKey);
  const selectedHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    // Focus a persistent heading so loading and failures are visible too. A small
    // focus target scrolls into view without centering an oversized payload panel.
    if (!targetSelected) selectedHeading.current?.focus();
  }, [selectedEvidence, run.id, targetSelected]);
  return (
    <article className="foundation-panel">
      <h2>Evidence</h2>
      <h3>Pack coverage</h3>
      {run.pack_coverage === null ? (
        <p>
          {["queued", "collecting"].includes(run.state)
            ? "Pack coverage is derived after every source is saved."
            : "Coverage was not captured for this run."}
        </p>
      ) : run.pack_coverage.length === 0 ? (
        <p>No pack coverage published.</p>
      ) : (
        <ul>
          {run.pack_coverage.map((pack) => (
            <li key={pack.pack}>
              <strong>{pack.pack}</strong> · {pack.status}
              {pack.missing_required.length > 0 &&
                ` · Missing required: ${pack.missing_required.join(", ")}`}
              {pack.optional_gaps.length > 0 &&
                ` · Optional gaps: ${pack.optional_gaps.join(", ")}`}
            </li>
          ))}
        </ul>
      )}
      <h3>Source catalog</h3>
      {!run.evidence.length ? (
        <p>
          No evidence has been published for this run. A failed collection is
          not successful evidence.
        </p>
      ) : (
        <ul className="foundation-stack evidence-sources">
          {run.evidence.map((e) => (
            <li key={e.id}>
              <a
                aria-current={selectedEvidence === e.id ? "page" : undefined}
                href={buildIncidentHash(
                  incidentId,
                  run.id,
                  returnTo,
                  selectedResolution,
                  e.id,
                  { ...safeRoute(window.location.hash).selection, locatorKind: undefined, locatorKey: undefined },
                )}
              >
                {e.source_key}
              </a>{" "}
              · {coverageLabel(e)} · content {e.content_status}
            </li>
          ))}
        </ul>
      )}
      {selectedEvidence && (
        <section aria-label="Selected evidence" className="foundation-stack">
          <h3 ref={selectedHeading} tabIndex={-1}>
            Selected evidence
          </h3>
          <button type="button" onClick={() => setShowTargets(!showTargets)} aria-expanded={showTargets || targetSelected}>Inspect stable citation targets</button>
          {(showTargets || targetSelected) && <CitationTarget key={`citation:${run.id}:${selectedEvidence}`} incidentId={incidentId} runId={run.id} evidenceId={selectedEvidence} />}
          <EvidenceDetail
            key={`${run.id}:${selectedEvidence}`}
            incidentId={incidentId}
            runId={run.id}
            evidenceId={selectedEvidence}
            record={run.evidence.find((row) => row.id === selectedEvidence)}
          />
        </section>
      )}
    </article>
  );
}
