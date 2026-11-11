import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { buildIncidentHash, incidentReturnHash, safeRoute } from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import type { components } from "@/lib/api/generated/schema";

type Kind = components["schemas"]["Locator"]["kind"];

export function CitationTarget({
  incidentId,
  runId,
  evidenceId,
}: {
  incidentId: string;
  runId: string;
  evidenceId: string;
}) {
  const route = safeRoute(window.location.hash);
  const selection = route.selection ?? {};
  const [cursor, setCursor] = useState<string | undefined>();
  const heading = useRef<HTMLHeadingElement>(null);
  const path = {
    incident_id: incidentId,
    run_id: runId,
    evidence_id: evidenceId,
  };
  const targets = useQuery({
    queryKey: ["citation-targets", incidentId, runId, evidenceId, cursor],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET(
          "/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/citation-targets",
          { signal, params: { path, query: { cursor, limit: 50 } } },
        ),
      ),
    retry: false,
    gcTime: 0,
  });
  const resolved = useQuery({
    queryKey: [
      "citation-target",
      incidentId,
      runId,
      evidenceId,
      selection.locatorKind,
      selection.locatorKey,
    ],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET(
          "/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}/citation",
          {
            signal,
            params: {
              path,
              query: {
                locator_kind: selection.locatorKind as Kind,
                locator_key: selection.locatorKey,
              },
            },
          },
        ),
      ),
    enabled: Boolean(selection.locatorKey),
    retry: false,
    gcTime: 0,
  });
  useEffect(() => {
    if (selection.locatorKey) {
      // Exact navigation must expose the target immediately; the document's
      // smooth scrolling otherwise leaves keyboard focus far outside view.
      heading.current?.focus({ preventScroll: true });
      heading.current?.scrollIntoView?.({ behavior: "instant", block: "center" });
    }
  }, [selection.locatorKey, resolved.data, resolved.error]);
  return (
    <section
      className="foundation-stack min-w-0"
      aria-label="Evidence citation targets"
    >
      <h3>Stored citation targets</h3>
      {selection.rightRun && (
        <a
          className="block max-w-full"
          href={buildIncidentHash(
            selection.leftIncident || incidentId,
            selection.leftRun || runId,
            incidentReturnHash(window.location.hash),
            !selection.leftIncident || selection.leftIncident === incidentId
              ? route.resolution
              : "",
            "",
            { ...selection, locatorKind: undefined, locatorKey: undefined },
          )}
        >
          Return to selected comparison
        </a>
      )}
      {selection.locatorKey && (
        <section
          className="foundation-panel min-w-0"
          aria-label="Exact citation target"
        >
          <h4 tabIndex={-1} ref={heading}>
            Exact citation target
          </h4>
          {resolved.isPending ? (
            <Loading />
          ) : resolved.isError ? (
            <Failure
              error={resolved.error}
              retry={() => void resolved.refetch()}
            />
          ) : (
            <>
              <p>{resolved.data.target?.label}</p>
              <pre className="evidence-json">
                {JSON.stringify(resolved.data.target?.value, null, 2)}
              </pre>
              {resolved.data.target?.preview_truncated && (
                <p>
                  Target preview truncated; full retained content is shown
                  above.
                </p>
              )}
              <p>
                Evidence: {resolved.data.evidence.id} · SHA-256:{" "}
                {resolved.data.evidence.sha256}
              </p>
            </>
          )}
        </section>
      )}
      {targets.isPending ? (
        <Loading />
      ) : targets.isError ? (
        <Failure error={targets.error} retry={() => void targets.refetch()} />
      ) : (
        <>
          {!targets.data.items.length && (
            <p>
              No stable targets captured for this payload. Inspect the retained
              source above.
            </p>
          )}
          <ul className="foundation-stack">
            {targets.data.items.map((target) => (
              <li key={target.locator.key}>
                {target.ambiguous ? (
                  <span>{target.label} · Ambiguous stored identity</span>
                ) : (
                  <a
                    href={buildIncidentHash(
                      incidentId,
                      runId,
                      incidentReturnHash(window.location.hash),
                      route.resolution,
                      evidenceId,
                      {
                        ...selection,
                        locatorKind: target.locator.kind,
                        locatorKey: target.locator.key,
                      },
                    )}
                  >
                    {target.label}
                  </a>
                )}
                {target.multiplicity > 1 && (
                  <span> · {target.multiplicity} retained occurrences</span>
                )}
              </li>
            ))}
          </ul>
          <div className="foundation-actions">
            <Button
              variant="outline"
              disabled={!cursor}
              onClick={() => setCursor(undefined)}
            >
              First targets
            </Button>
            <Button
              variant="outline"
              disabled={!targets.data.next_cursor}
              onClick={() => setCursor(targets.data.next_cursor || undefined)}
            >
              More targets
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
