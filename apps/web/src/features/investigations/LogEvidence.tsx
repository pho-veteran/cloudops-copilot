export function LogEvidence({ payload }: { payload: Record<string, unknown> }) {
  const entries = Array.isArray(payload.logs) ? payload.logs : [];
  const logs = entries.flatMap((value) => {
    if (!value || typeof value !== "object") return [];
    const row = value as Record<string, unknown>;
    const text = typeof row.text === "string"
      ? row.text
      : Array.isArray(row.lines) && row.lines.every((line) => typeof line === "string")
        ? row.lines.join("\n")
        : null;
    return text === null ? [] : [{ row, text }];
  });
  return (
    <>
      {!logs.length && <p>No text log segments captured; the raw stored payload remains available.</p>}
      {logs.map(({ row, text }, index) => {
        const name = typeof row.container === "string" ? row.container : "Container not captured";
        return (
          <section key={index} aria-label={`Logs for ${name}`}>
            <h4>{name}</h4>
            <p>
              {row.kind === "init" ? "Init container" : row.kind === "regular" ? "Regular container" : "Container kind not captured"}
              {" · "}{row.previous === true ? "Previous logs" : row.previous === false ? "Current logs" : "Log instance not captured"}
            </p>
            <p>Pod UID: <span className="mono">{typeof row.pod_uid === "string" ? row.pod_uid : "Not captured"}</span></p>
            <pre className="evidence-json evidence-log-text" tabIndex={0} aria-label={`Log text for ${name}`}>{text}</pre>
          </section>
        );
      })}
      <details>
        <summary>Raw stored payload</summary>
        <pre className="evidence-json">{JSON.stringify(payload, null, 2)}</pre>
      </details>
    </>
  );
}
