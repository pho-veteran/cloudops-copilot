import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { Failure } from "@/app/Feedback";

type Evidence = components["schemas"]["EvidenceView"];
type Content = components["schemas"]["EvidenceContent"];
type RequestWindow = components["schemas"]["RequestWindow"];
type Marker = components["schemas"]["TimelineMarker"];

type Sample = { at: number; value: number };
type Series = {
  uid: string;
  name: string;
  pod: string | null;
  container: string | null;
  unit: string;
  samples: Sample[];
};

const COLORS = [
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-1)",
];
const WIDTH = 640;
const HEIGHT = 240;
const PADDING = { left: 58, right: 18, top: 18, bottom: 36 };
const KINDS: Record<string, string> = {
  window_start: "Window start",
  window_end: "Window end",
  rollout_observation: "Rollout observation",
  incident_activity: "Incident activity",
  execution_created: "Recovery execution created",
  execution_finished: "Recovery execution finished",
};

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function numeric(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function timeMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.abs(value) < 1e12 ? value * 1000 : value;
  }
  if (typeof value === "string") {
    const number = Number(value);
    if (Number.isFinite(number)) return Math.abs(number) < 1e12 ? number * 1000 : number;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function sample(row: unknown): Sample | null {
  if (!Array.isArray(row) || row.length !== 2) return null;
  const at = timeMs(row[0]);
  const value = numeric(row[1]);
  return at === null || value === null ? null : { at, value };
}

function seriesFrom(payload: Record<string, unknown>): Series[] {
  const rows = Array.isArray(payload.series) ? payload.series : [];
  return rows.flatMap((row, index) => {
    if (!row || typeof row !== "object") return [];
    const value = row as Record<string, unknown>;
    const raw = Array.isArray(value.samples)
      ? value.samples
      : Array.isArray(value.values)
        ? value.values
        : [];
    const samples = raw
      .map(sample)
      .filter((item): item is Sample => item !== null)
      .sort((left, right) => left.at - right.at);
    return [
      {
        uid: text(value.uid) ?? `series-${index}`,
        name: text(value.name) ?? "Metric series",
        pod: text(value.pod),
        container: text(value.container),
        unit: text(value.unit) ?? "value",
        samples,
      },
    ];
  });
}

function seriesLabel(series: Series): string {
  const context = [series.pod, series.container].filter(Boolean).join(" / ");
  return context ? `${series.name} · ${context}` : series.name;
}

function windowMs(
  window: RequestWindow | null,
  metadata: Record<string, unknown> | null,
): { start: number; end: number } | null {
  const embedded = metadata?.window;
  if (embedded && typeof embedded === "object") {
    const value = embedded as Record<string, unknown>;
    const start = timeMs(value.start);
    const end = timeMs(value.end);
    if (start !== null && end !== null && start < end) return { start, end };
  }
  if (window) {
    const start = timeMs(window.start);
    const end = timeMs(window.end);
    if (start !== null && end !== null && start < end) return { start, end };
  }
  return null;
}

function formatTime(value: number): string {
  return new Date(value).toLocaleString();
}

function detailText(detail: Record<string, unknown> | undefined): string {
  if (!detail) return "";
  const values = Object.entries(detail)
    .filter(([, value]) => typeof value === "string" || typeof value === "number")
    .map(([key, value]) => `${key}: ${String(value)}`);
  return values.join(" · ");
}

function Chart({
  series,
  window,
  markers,
  color,
}: {
  series: Series;
  window: { start: number; end: number } | null;
  markers: Marker[];
  color: string;
}) {
  const samples = series.samples;
  const domain = useMemo(() => {
    const x = window ?? {
      start: samples[0]?.at ?? 0,
      end: samples.at(-1)?.at ?? 1,
    };
    const values = samples.map((item) => item.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    return {
      minX: x.start,
      maxX: x.end === x.start ? x.end + 1 : x.end,
      minY: min === max ? min - 1 : min,
      maxY: min === max ? max + 1 : max,
    };
  }, [samples, window]);
  if (!samples.length) return null;
  const innerWidth = WIDTH - PADDING.left - PADDING.right;
  const innerHeight = HEIGHT - PADDING.top - PADDING.bottom;
  const x = (at: number) =>
    PADDING.left + ((at - domain.minX) / (domain.maxX - domain.minX)) * innerWidth;
  const y = (value: number) =>
    PADDING.top +
    innerHeight -
    ((value - domain.minY) / (domain.maxY - domain.minY)) * innerHeight;
  const visible = markers.flatMap((marker) => {
    const at = timeMs(marker.at);
    return at !== null && at >= domain.minX && at <= domain.maxX ? [marker] : [];
  });
  return (
    <div className="metric-chart-frame">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`${seriesLabel(series)} over ${formatTime(domain.minX)} to ${formatTime(domain.maxX)}`}
      >
        {[0, 1, 2, 3].map((line) => {
          const position = PADDING.top + (innerHeight / 3) * line;
          const value = domain.maxY - ((domain.maxY - domain.minY) / 3) * line;
          return (
            <g key={line}>
              <line
                x1={PADDING.left}
                x2={WIDTH - PADDING.right}
                y1={position}
                y2={position}
                stroke="currentColor"
                opacity=".12"
              />
              <text x={4} y={position + 4}>
                {value.toLocaleString(undefined, { maximumFractionDigits: 3 })}
              </text>
            </g>
          );
        })}
        {visible.map((marker) => {
          const at = timeMs(marker.at);
          if (at === null) return null;
          return (
            <line
              key={`${marker.kind}:${marker.id}:${marker.at}`}
              x1={x(at)}
              x2={x(at)}
              y1={PADDING.top}
              y2={HEIGHT - PADDING.bottom}
              stroke="var(--muted-foreground)"
              strokeDasharray="3 3"
              opacity=".55"
            >
              <title>
                {KINDS[marker.kind] ?? marker.kind} · {formatTime(at)}
              </title>
            </line>
          );
        })}
        {samples.length > 1 ? (
          <polyline
            points={samples.map((item) => `${x(item.at)},${y(item.value)}`).join(" ")}
            fill="none"
            stroke={color}
            strokeWidth="2.5"
          />
        ) : (
          <circle cx={x(samples[0].at)} cy={y(samples[0].value)} r="3" fill={color} />
        )}
        <text x={PADDING.left} y={HEIGHT - 10}>
          {formatTime(domain.minX)}
        </text>
        <text x={WIDTH - PADDING.right} y={HEIGHT - 10} textAnchor="end">
          {formatTime(domain.maxX)}
        </text>
        <text x={WIDTH - PADDING.right} y={12} textAnchor="end" className="metric-unit">
          {series.unit}
        </text>
      </svg>
    </div>
  );
}

function TimelinePanel({
  incidentId,
  runId,
  markers,
  error,
  retry,
}: {
  incidentId: string;
  runId: string;
  markers: Marker[];
  error: unknown;
  retry: () => void;
}) {
  if (error) return <Failure error={error} retry={retry} />;
  return (
    <section className="metric-timeline" aria-labelledby="metric-timeline-heading">
      <h4 id="metric-timeline-heading">Timeline markers</h4>
      <p className="muted">
        Persisted records aligned by time. Alignment is correlation, not causality.
      </p>
      {!markers.length ? (
        <p>No persisted marker falls inside this window.</p>
      ) : (
        <ol className="metric-marker-list">
          {markers.map((marker) => {
            const at = timeMs(marker.at);
            return (
              <li key={`${marker.kind}:${marker.id}:${marker.at}`}>
                <time dateTime={marker.at}>{at === null ? marker.at : formatTime(at)}</time> ·{" "}
                <strong>{KINDS[marker.kind] ?? marker.kind}</strong>
                {marker.label ? ` · ${marker.label}` : ""}
                {detailText(marker.detail) ? ` · ${detailText(marker.detail)}` : ""}
              </li>
            );
          })}
        </ol>
      )}
      <p className="muted">
        Run {runId} · incident {incidentId}
      </p>
    </section>
  );
}

export function MetricEvidence({
  incidentId,
  runId,
  evidence,
  content,
  window: requestedWindow,
}: {
  incidentId: string;
  runId: string;
  evidence: Evidence;
  content: Content;
  window: RequestWindow | null;
}) {
  const timeline = useQuery({
    queryKey: ["run-timeline", incidentId, runId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/runs/{run_id}/timeline", {
          params: { path: { incident_id: incidentId, run_id: runId } },
        }),
      ),
    retry: false,
  });
  const series = useMemo(() => seriesFrom(content.payload), [content]);
  const [selected, setSelected] = useState(0);
  const chosen = series[Math.min(selected, Math.max(series.length - 1, 0))];
  const metadata =
    evidence.query_metadata && typeof evidence.query_metadata === "object"
      ? evidence.query_metadata
      : null;
  const warnings = Array.isArray(metadata?.warnings)
    ? metadata.warnings.filter((item): item is string => typeof item === "string")
    : [];
  const window = windowMs(requestedWindow, metadata);
  const markers = timeline.data?.markers ?? [];
  const hasSamples = series.some((item) => item.samples.length > 0);
  const ownershipUnproven = evidence.coverage.reason_codes.includes("ownership_unproven");

  return (
    <section className="metric-evidence" aria-labelledby="metric-evidence-heading">
      <h3 id="metric-evidence-heading">Stored metric series</h3>
      <p>
        {evidence.source_key} · {evidence.coverage.status}
        {evidence.coverage.reason_codes.length
          ? ` · ${evidence.coverage.reason_codes.join(", ")}`
          : ""}
      </p>
      {ownershipUnproven && (
        <p className="metric-warning">
          Returned series did not match the configured workload UID; no value is aligned from
          those series.
        </p>
      )}
      {warnings.map((warning) => (
        <p className="metric-warning" key={warning}>
          {warning}
        </p>
      ))}
      {!hasSamples ? (
        <p>
          No finite samples were returned for this source. Missing data is not zero and no
          line is drawn.
        </p>
      ) : (
        <>
          {series.length > 1 && (
            <div className="metric-series" role="group" aria-label="Metric series">
              {series.map((item, index) => (
                <button
                  key={item.uid}
                  type="button"
                  className="metric-series-button"
                  aria-pressed={index === selected}
                  onClick={() => setSelected(index)}
                >
                  {seriesLabel(item)}
                </button>
              ))}
            </div>
          )}
          {chosen && (
            <>
              <Chart
                series={chosen}
                window={window}
                markers={markers}
                color={COLORS[Math.min(selected, COLORS.length - 1)]}
              />
              <div className="metric-legend">
                <span
                  className="metric-swatch"
                  style={{ background: COLORS[Math.min(selected, COLORS.length - 1)] }}
                  aria-hidden="true"
                />
                <span>{seriesLabel(chosen)}</span>
                <span className="muted">
                  {chosen.samples.length} samples · {chosen.unit}
                </span>
              </div>
              <div className="metric-table-wrap">
                <table className="metric-table">
                  <caption>
                    {seriesLabel(chosen)} retained samples; timestamps are rendered in the
                    browser's local time.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Time</th>
                      <th scope="col">Value ({chosen.unit})</th>
                    </tr>
                  </thead>
                  <tbody>
                    {chosen.samples.map((item, index) => (
                      <tr key={`${index}:${item.at}:${item.value}`}>
                        <td>{formatTime(item.at)}</td>
                        <td>{item.value.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
      <details>
        <summary>Query metadata</summary>
        <pre className="evidence-json">
          {JSON.stringify(evidence.query_metadata ?? {}, null, 2)}
        </pre>
      </details>
      <TimelinePanel
        incidentId={incidentId}
        runId={runId}
        markers={markers}
        error={timeline.isError ? timeline.error : null}
        retry={() => void timeline.refetch()}
      />
    </section>
  );
}
