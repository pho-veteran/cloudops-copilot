import { useEffect, useMemo, useState } from "react";
import type { components } from "@/lib/api/generated/schema";
import { Badge } from "@/components/ui/badge";

type Summary = components["schemas"]["ObservationSummary"];

export function useAgedObservation(summary?: Summary | null) {
  const anchor = useMemo(() => ({ summary, received: performance.now() }), [summary]);
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick(v => v + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const age = summary?.age_seconds == null ? null : summary.age_seconds + (performance.now() - anchor.received) / 1000;
  const stale = age !== null && age > (summary?.stale_after_seconds ?? 60);
  const attemptExpired = summary?.refresh_remaining_seconds != null &&
    (performance.now() - anchor.received) / 1000 > summary.refresh_remaining_seconds;
  return {
    age,
    health: stale || attemptExpired ? "unknown" : summary?.effective_health ?? "unknown",
    freshness: stale ? "stale" : summary?.freshness ?? "unknown",
  };
}

export function WorkloadHealth({ summary }: { summary?: Summary | null }) {
  const aged = useAgedObservation(summary);
  return <div className="foundation-stack">
    <div className="foundation-actions" role="status">
      <Badge variant="outline">Kubernetes: {aged.health.replaceAll("_", " ")}</Badge>
      <Badge variant="outline">Freshness: {aged.freshness}</Badge>
    </div>
    <p className="muted">{aged.age === null ? "No complete live observation" : `Last complete observation ${Math.floor(aged.age)}s ago`}
      {summary?.refreshing ? " · Refreshing" : ""}</p>
    <p>Readiness: {aged.health === "unknown" || summary?.ready == null ? "Unknown" : `${summary.ready} / ${summary.desired ?? "unknown"}`}
      {" · "}Coverage: {summary?.coverage ?? "unknown"}</p>
    {!!summary?.reasons?.length && <p className="muted">{summary.reasons.map(v => v.replaceAll("_", " ")).join(" · ")}</p>}
    {(summary?.poll_lag_seconds ?? 0) > 30 && <p className="muted">Observer is behind schedule; displayed data may be incomplete.</p>}
    <p className="muted">Application and traffic health are unverified.</p>
  </div>;
}
