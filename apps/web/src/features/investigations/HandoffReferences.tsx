import {
  buildIncidentHash,
  buildKnowledgeHash,
  incidentReturnHash,
} from "@/app/routes";

const uuid = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;
type RecordValue = Record<string, unknown>;
function object(value: unknown): RecordValue | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as RecordValue)
    : null;
}

/** Structured saved metadata only. Never parse a report's rendered Markdown. */
export function storedReferences(snapshot: unknown) {
  const root = object(snapshot);
  const evidence = object(root?.evidence);
  const citations = object(root?.citations);
  const declared: unknown[] = Array.isArray(evidence?.items)
    ? evidence.items
    : [];
  const byId = new Map<
    string,
    { run: string; evidence: string; kind?: string; key?: string }
  >();
  for (const value of declared) {
    const row = object(value);
    if (
      typeof row?.id !== "string" ||
      typeof row.run_id !== "string" ||
      !uuid.test(row.id) ||
      !uuid.test(row.run_id)
    )
      continue;
    byId.set(`${row.run_id}:${row.id}`, { run: row.run_id, evidence: row.id });
  }
  const refs = Array.isArray(citations?.items) ? citations.items : [];
  for (const value of refs) {
    const row = object(value);
    if (typeof row?.run_id !== "string" || typeof row.evidence_id !== "string")
      continue;
    // A citation must reference a source declared in this very snapshot.
    const key = `${row.run_id}:${row.evidence_id}`;
    const source = byId.get(key);
    if (!source) continue;
    const locator = object(row.locator);
    if (
      locator &&
      typeof locator.kind === "string" &&
      ["field", "event", "log_segment", "series", "sample"].includes(
        locator.kind,
      ) &&
      typeof locator.key === "string" &&
      /^v1_[a-f0-9]{64}$/.test(locator.key)
    ) {
      byId.set(`${key}:${locator.kind}:${locator.key}`, {
        ...source,
        kind: locator.kind,
        key: locator.key,
      });
    }
  }
  return [...byId.values()];
}

export function storedRunbooks(snapshot: unknown) {
  const root = object(snapshot);
  const runs = object(root?.runs);
  const declared = new Set(
    (Array.isArray(runs?.items) ? runs.items : []).map(
      (row: unknown) => object(row)?.id,
    ),
  );
  const section = object(root?.runbook_citations);
  return (Array.isArray(section?.items) ? section.items : []).flatMap(
    (value: unknown) => {
      const row = object(value);
      const ref = object(row?.reference);
      if (
        !row ||
        !ref ||
        typeof row.run_id !== "string" ||
        !declared.has(row.run_id) ||
        ![
          row.run_id,
          row.selection_id,
          ref.document_version_id,
          ref.chunk_id,
        ].every((id) => typeof id === "string" && uuid.test(id))
      )
        return [];
      return [
        {
          run: row.run_id,
          selection: row.selection_id as string,
          document: ref.document_version_id as string,
          chunk: ref.chunk_id as string,
          title: String(ref.document_key),
          version: String(ref.version),
        },
      ];
    },
  );
}

export function HandoffReferences({
  incidentId,
  resolutionId,
  snapshot,
}: {
  incidentId: string;
  resolutionId: string;
  snapshot: unknown;
}) {
  const refs = storedReferences(snapshot);
  const runbooks = storedRunbooks(snapshot);
  return (
    <section
      aria-label="Stored handoff evidence references"
      className="foundation-stack min-w-0 break-words"
    >
      <h4>
        {resolutionId
          ? "Saved handoff evidence references"
          : "Current handoff evidence references"}
      </h4>
      {!refs.length ? (
        <p>
          Structured evidence references not captured. No latest-Run substitute
          is used.
        </p>
      ) : (
        <ul className="foundation-stack">
          {refs.map((ref) => (
            <li key={`${ref.run}:${ref.evidence}:${ref.key || "source"}`}>
              <a
                className="block max-w-full"
                href={buildIncidentHash(
                  incidentId,
                  ref.run,
                  incidentReturnHash(window.location.hash),
                  resolutionId,
                  ref.evidence,
                  { locatorKind: ref.kind, locatorKey: ref.key },
                )}
              >
                Open stored {ref.kind ? `${ref.kind} citation` : "evidence"} ·{" "}
                {ref.evidence}
              </a>
              <p>Run: {ref.run}</p>
            </li>
          ))}
        </ul>
      )}
      {runbooks.length > 0 && (
        <>
          <h4>Stored runbook methodology</h4>
          <ul>
            {runbooks.map((ref, i) => (
              <li key={i}>
                <a
                  href={buildKnowledgeHash({
                    incident: incidentId,
                    run: ref.run,
                    document: ref.document,
                    chunk: ref.chunk,
                    selection: ref.selection,
                    returnTo: window.location.hash,
                  })}
                >
                  {ref.title} · version {ref.version} · stored selection
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
