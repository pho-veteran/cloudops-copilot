import type { components } from "@/lib/api/generated/schema";

type Report = components["schemas"]["ReportView"];
export const reportId = (index: number) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
export const reportWorkload = {
  id: reportId(1), cluster: "controlled-display-fixture", namespace: "demo", name: "checkout",
  uid: "controlled-deployment-uid", active: true, source: "live_kubernetes", health: "healthy",
} satisfies components["schemas"]["WorkloadView"];

export function reportFixture(index = 99): Report {
  const identity: components["schemas"]["ReportIdentity"] = {
    workload_id: reportWorkload.id, cluster: reportWorkload.cluster, namespace: reportWorkload.namespace,
    name: reportWorkload.name, uid: reportWorkload.uid, containers: ["app"],
    observed_at: "2026-11-17T06:00:00Z", cluster_fingerprint: "a".repeat(64),
    template_digest: "b".repeat(64), pod_template_digest: "c".repeat(64), resources_digest: "d".repeat(64),
  };
  const profile: components["schemas"]["ReportProfile"] = { history_hours: 6, image_digest: `sha256:${"e".repeat(64)}`, tool_version: "1.31.1", strategy: "simple", cpu_percentile: 95, memory_buffer_percent: 15, timeframe_minutes: 1.25, points_required: 100, allow_hpa: false, use_oomkill_data: false, minimum_cpu_millicores: 10, minimum_memory_bytes: 104857600, policy_version: "e08-coverage-v1" };
  const coverage = { expected_bins: 360, covered_bins: 360, ratio: 1, longest_gap_seconds: 0, first_sample_at: "2026-11-17T00:00:00Z", last_sample_at: "2026-11-17T06:00:00Z", reasons: [] };
  return {
    id: reportId(index), workload_id: reportWorkload.id, schema_version: 2,
    state: "available", requested_hours: 6, tool_version: "krr-v1.31.1", result: { recommendations: null },
    reason: "Controlled display fixture; no live recommendation quality is established.",
    created_by: reportId(2), created_at: "2026-11-17T06:00:00Z", finished_at: "2026-11-17T06:01:00Z",
    input_identity: identity, profile, migration_disposition: null,
    job_phase: "finished", result_state: "available", attempts: [], dispatch_paused: false, dispatch_reason: null,
    applicability: { state: "stale", assessed_at: "2026-11-17T07:00:00Z", reasons: ["template_changed"] },
    export_ready: true, export_sha256: "f".repeat(64),
    result_v2: {
      schema_version: 2,
      state: "available", reason: "qualified_advice", source: "live_krr", identity, profile,
      window: { kind: "query_envelope", start: "2026-11-17T00:00:00Z", end: "2026-11-17T06:00:00Z", raw_input_start: "2026-11-16T23:58:45Z", raw_input_end: "2026-11-17T06:00:00Z" },
      tool_exit_code: 0, output_sha256: "1".repeat(64), query_digest: "2".repeat(64),
      limitations: ["Controlled display fixture. It is not live six-hour evidence."],
      resources: [
        { container: "app", resource: "cpu", state: "available", current_request: { kind: "quantity", value: "0.2", unit: "cores" }, current_limit: { kind: "unset" }, observed: { kind: "quantity", value: "0", unit: "cores" }, recommended_request: { kind: "quantity", value: "0.01", unit: "cores" }, recommended_limit: { kind: "unset" }, statistic: "percentile_95", statistic_definition: "Maximum of per-Pod 95th percentiles of 75-second CPU rates", buffer_percent: 0, minimum: { kind: "quantity", value: "0.01", unit: "cores" }, rounding: "Upward to 1 millicore", coverage, reasons: [] },
        { container: "app", resource: "memory", state: "excluded", current_request: { kind: "unset" }, current_limit: { kind: "quantity", value: "167772160", unit: "bytes" }, observed: { kind: "quantity", value: "25165824", unit: "bytes" }, recommended_request: { kind: "unavailable", reason: "recent_oom" }, recommended_limit: { kind: "unavailable", reason: "recent_oom" }, statistic: "maximum", buffer_percent: 15, minimum: { kind: "quantity", value: "104857600", unit: "bytes" }, rounding: "Upward to 1 MiB", coverage, reasons: ["recent_oom"] },
      ],
    },
  };
}
