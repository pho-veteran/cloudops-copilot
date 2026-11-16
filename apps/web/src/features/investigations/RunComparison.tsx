import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import {
  buildIncidentHash,
  incidentReturnHash,
  safeRoute,
  type EvidenceSelection,
} from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { download } from "@/lib/download";
import type { components } from "@/lib/api/generated/schema";
import { coverageLabel } from "./EvidenceCatalog";
import { AnalysisSummary } from "@/features/diagnosis/Analysis";

type Side = components["schemas"]["ComparisonSide"];
type Citation = components["schemas"]["CitationRef"];
const uuid = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;

function SideSummary({ label, side }: { label: string; side: Side }) {
  return (
    <section className="min-w-0 break-words">
      <h3>{label}</h3>
      <p className="mono">{side.run_id}</p>
      <p>
        {side.state} · {side.namespace}/{side.name}
      </p>
      <p>Question: {side.question || "Not captured"}</p>
      <p>
        Workload: {side.workload_id}
        <br />
        UID: {side.workload_uid}
      </p>
      <p>
        Window:{" "}
        {side.window
          ? `${side.window.start} → ${side.window.end}`
          : "Not captured"}
      </p>
      {side.analysis ? (
        <AnalysisSummary analysis={side.analysis} />
      ) : (
        <p>No analysis was recorded for this selection.</p>
      )}
    </section>
  );
}

export function RunComparison({
  incidentId,
  runId,
}: {
  incidentId: string;
  runId: string;
}) {
  const route = safeRoute(window.location.hash);
  const selected = route.selection ?? {};
  const leftIncident = selected.leftIncident || incidentId;
  const leftRun = selected.leftRun || runId;
  const [draft, setDraft] = useState({
    leftIncident,
    leftRun,
    rightIncident: selected.rightIncident || incidentId,
    rightRun: selected.rightRun || "",
  });
  const [cursor, setCursor] = useState<string | undefined>();
  const heading = useRef<HTMLHeadingElement>(null);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const enabled = Boolean(selected.rightIncident && selected.rightRun);
  const query = useQuery({
    queryKey: [
      "run-comparison",
      leftIncident,
      leftRun,
      selected.rightIncident,
      selected.rightRun,
      selected.source,
      selected.catalog,
      selected.changedOnly,
      selected.differenceStatus,
      cursor,
    ],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs/{run_id}/compare", {
          signal,
          params: {
            path: { incident_id: leftIncident, run_id: leftRun },
            query: {
              right_incident_id: selected.rightIncident!,
              right_run_id: selected.rightRun!,
              source_key: selected.source,
              catalog_version: selected.catalog,
              changed_only: selected.changedOnly || false,
              difference_status: selected.differenceStatus as
                | components["schemas"]["SemanticDifference"]["status"]
                | undefined,
              cursor,
              limit: 50,
            },
          },
        }),
      ),
    enabled,
    retry: false,
    gcTime: 0,
  });
  useEffect(() => {
    if (query.isSuccess && !route.evidence)
      (selected.source ? detailHeading : heading).current?.focus();
  }, [query.isSuccess, selected.source, cursor, route.evidence]);
  const navigate = (
    selection: EvidenceSelection,
    targetIncident = leftIncident,
    targetRun = leftRun,
    evidence = "",
  ) => {
    setCursor(undefined);
    window.location.hash = buildIncidentHash(
      targetIncident,
      targetRun,
      incidentReturnHash(window.location.hash),
      targetIncident === incidentId ? route.resolution : "",
      evidence,
      { plan: selected.plan, execution: selected.execution, ...selection },
    );
  };
  const cite = (ref: Citation, side: Side) =>
    buildIncidentHash(
      side.incident_id,
      ref.run_id,
      incidentReturnHash(window.location.hash),
      side.incident_id === incidentId ? route.resolution : "",
      ref.evidence_id,
      {
        ...selected,
        leftIncident,
        leftRun,
        locatorKind: ref.locator?.kind,
        locatorKey: ref.locator?.key,
      },
    );
  return (
    <article
      className="foundation-panel foundation-stack min-w-0 break-words"
      aria-label="Compare stored runs"
    >
      <h2 ref={heading} tabIndex={-1}>
        Compare runs
      </h2>
      <p>
        Compare stored observations. Missing data is not zero; differences do
        not establish causality.
      </p>
      <form
        className="foundation-stack"
        onSubmit={(event) => {
          event.preventDefault();
          if (Object.values(draft).every((value) => uuid.test(value)))
            navigate({ ...draft });
        }}
      >
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          {(["left", "right"] as const).map((side) => (
            <fieldset key={side} className="min-w-0 foundation-stack">
              <legend>{side === "left" ? "Left" : "Right"} selection</legend>
              {(["Incident", "Run"] as const).map((part) => {
                const field = `${side}${part}` as keyof typeof draft;
                return (
                  <div key={field}>
                    <Label htmlFor={`compare-${field}`}>
                      {side === "left" ? "Left" : "Right"} {part.toLowerCase()}{" "}
                      ID
                    </Label>
                    <input
                      className="w-full min-w-0 rounded border bg-transparent p-2"
                      id={`compare-${field}`}
                      value={draft[field]}
                      maxLength={36}
                      required
                      pattern="[a-fA-F0-9-]{36}"
                      onChange={(event) =>
                        setDraft({ ...draft, [field]: event.target.value })
                      }
                    />
                  </div>
                );
              })}
            </fieldset>
          ))}
        </div>
        <Button
          type="submit"
          disabled={!Object.values(draft).every((value) => uuid.test(value))}
        >
          Compare selected runs
        </Button>
      </form>
      {!enabled ? (
        <p>
          Select both Runs explicitly. Run IDs from older history pages can be
          entered directly.
        </p>
      ) : query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <div className="foundation-actions">
            <Button
              variant="outline"
              onClick={() =>
                navigate(
                  {
                    leftIncident: selected.rightIncident,
                    leftRun: selected.rightRun,
                    rightIncident: leftIncident,
                    rightRun: leftRun,
                    changedOnly: selected.changedOnly,
                    differenceStatus: selected.differenceStatus,
                    source: selected.source,
                    catalog: selected.catalog,
                  },
                  selected.rightIncident,
                  selected.rightRun,
                )
              }
            >
              Swap left and right
            </Button>
            <Button
              variant="outline"
              onClick={() =>
                navigate({
                  ...selected,
                  leftIncident,
                  leftRun,
                  changedOnly: !selected.changedOnly,
                  differenceStatus: undefined,
                })
              }
            >
              {selected.changedOnly
                ? "Show all observations"
                : "Show differences only"}
            </Button>
            <Button
              variant="outline"
              onClick={() =>
                download(
                  JSON.stringify(
                    {
                      ...query.data,
                      export_scope:
                        "Currently loaded source/page; next_cursor indicates remaining rows.",
                    },
                    null,
                    2,
                  ),
                  `comparison-${leftRun}-${selected.rightRun}.json`,
                  "application/json",
                )
              }
            >
              Export loaded comparison
            </Button>
            <a
              href={buildIncidentHash(
                leftIncident,
                leftRun,
                incidentReturnHash(window.location.hash),
                leftIncident === incidentId ? route.resolution : "",
              )}
            >
              Return to Run evidence
            </a>
          </div>
          <div>
            <Label htmlFor="comparison-status">Difference status</Label>
            <select
              id="comparison-status"
              className="max-w-full rounded border bg-transparent p-2"
              value={selected.differenceStatus || ""}
              onChange={(event) =>
                navigate({
                  ...selected,
                  leftIncident,
                  leftRun,
                  changedOnly: false,
                  differenceStatus: event.target.value || undefined,
                })
              }
            >
              <option value="">All difference states</option>
              {[
                "added",
                "removed",
                "changed",
                "unchanged",
                "unavailable",
                "not_comparable",
              ].map((status) => (
                <option value={status} key={status}>
                  {status.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="grid min-w-0 gap-4 md:grid-cols-2">
            <SideSummary label="Left Run" side={query.data.left} />
            <SideSummary label="Right Run" side={query.data.right} />
          </div>
          {query.data.reason && (
            <p role="status">{query.data.reason.replaceAll("_", " ")}</p>
          )}
          {query.data.identity_relation === "replacement" && (
            <p>
              Workload identity replaced. Values are not aligned across UIDs.
            </p>
          )}
          <h3>Sources and coverage</h3>
          {!query.data.sources.length && <p>No alignable stored sources.</p>}
          <ul className="foundation-stack">
            {query.data.sources.map((source) => (
              <li key={`${source.source_key}:${source.catalog_version}`}>
                <a
                  className="block max-w-full"
                  href={buildIncidentHash(
                    leftIncident,
                    leftRun,
                    incidentReturnHash(window.location.hash),
                    leftIncident === incidentId ? route.resolution : "",
                    "",
                    {
                      ...selected,
                      leftIncident,
                      leftRun,
                      source: source.source_key,
                      catalog: source.catalog_version,
                    },
                  )}
                >
                  {source.source_key} · {source.catalog_version}
                </a>
                <p>
                  {source.status} {source.reason?.replaceAll("_", " ")}
                </p>
                <p>
                  Left:{" "}
                  {source.left
                    ? `${coverageLabel(source.left)} · ${source.left.content_status}`
                    : "Not recorded"}{" "}
                  · Right:{" "}
                  {source.right
                    ? `${coverageLabel(source.right)} · ${source.right.content_status}`
                    : "Not recorded"}
                </p>
                <details>
                  <summary>Stored source provenance</summary>
                  {(["left", "right"] as const).map((side) => (
                    <div key={side}>
                      <strong>{side === "left" ? "Left" : "Right"}</strong>
                      <p>Evidence: {source[side]?.id || "Not recorded"}</p>
                      <p>SHA-256: {source[side]?.sha256 || "Not captured"}</p>
                      <p>
                        Coverage reasons:{" "}
                        {source[side]?.coverage.reason_codes.join(", ") ||
                          "None recorded"}
                      </p>
                    </div>
                  ))}
                </details>
              </li>
            ))}
          </ul>
          {selected.source && (
            <section
              className="foundation-stack"
              aria-label="Source differences"
            >
              <h3 ref={detailHeading} tabIndex={-1}>
                {selected.source} differences
              </h3>
              {!query.data.items.length && (
                <p>
                  No matching comparable observations on this page. Inspect
                  source availability above.
                </p>
              )}
              {query.data.items.map((item) => (
                <section className="foundation-panel min-w-0" key={item.key}>
                  <h4>{item.label}</h4>
                  <p>
                    {item.status} · {item.reason?.replaceAll("_", " ")}
                  </p>
                  <div className="grid min-w-0 gap-4 md:grid-cols-2">
                    {(["left", "right"] as const).map((side) => {
                      const ref = item[`${side}_citation`];
                      return (
                        <section key={side} className="min-w-0">
                          <h5>{side === "left" ? "Left" : "Right"}</h5>
                          <pre className="evidence-json">
                            {ref
                              ? JSON.stringify(item[side], null, 2)
                              : "Not observed"}
                          </pre>
                          {(item[`${side}_occurrences`] ?? 0) > 1 && (
                            <p>
                              {item[`${side}_occurrences`]} retained occurrences
                            </p>
                          )}
                          {ref && (
                            <a
                              className="block max-w-full"
                              href={cite(ref, query.data[side])}
                            >
                              {ref.locator
                                ? `Open exact ${side} evidence target`
                                : `Open ${side} stored source`}
                            </a>
                          )}
                        </section>
                      );
                    })}
                  </div>
                  {item.preview_truncated && (
                    <p>
                      Preview truncated. Open the retained source for context.
                    </p>
                  )}
                </section>
              ))}
              <div className="foundation-actions">
                <Button
                  variant="outline"
                  disabled={!cursor}
                  onClick={() => setCursor(undefined)}
                >
                  First page
                </Button>
                <Button
                  variant="outline"
                  disabled={!query.data.next_cursor}
                  onClick={() => setCursor(query.data.next_cursor || undefined)}
                >
                  Next differences
                </Button>
              </div>
            </section>
          )}
        </>
      )}
    </article>
  );
}
