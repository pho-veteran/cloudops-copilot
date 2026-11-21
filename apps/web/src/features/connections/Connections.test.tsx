// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { components } from "@/lib/api/generated/schema";
import { Connections } from "./Connections";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { GET: get },
  message: (error: unknown) => String(error),
  unwrap: ({ data }: { data: unknown }) => data,
}));
vi.mock("./AlertReceipts", () => ({ AlertReceipts: () => null }));
vi.mock("@/features/diagnosis/ModelConnection", () => ({ ModelConnection: () => null }));

it("shows unobserved and denied states and links to workload authority without polling it", async () => {
  const assessedAt = "2026-11-18T01:02:03Z";
  const reportId = "00000000-0000-4000-8000-000000000108";
  const workloadId = "00000000-0000-4000-8000-000000000104";
  const health: components["schemas"]["HealthView"] = {
    collection_dispatch: { status: "paused", reason_code: "class_denied" },
    observations: [
      {
        component: "worker",
        source: "runtime",
        status: "never_observed",
        assessed_at: assessedAt,
        observed_at: null,
        reason_code: "heartbeat_missing",
        reason: "The worker has not recorded a heartbeat.",
      },
      {
        component: "kubernetes",
        source: "live_kubernetes",
        status: "denied",
        assessed_at: assessedAt,
        last_success_at: "2026-11-18T01:02:00Z",
        last_failure_at: assessedAt,
        reason_code: "observation_denied",
        reason: "The Kubernetes reader was denied.",
        target: {
          workload_id: workloadId,
          cluster: "lab",
          namespace: "demo",
          name: "checkout",
          uid: "deployment-uid",
        },
      },
      {
        component: "executor",
        source: "feature_view",
        status: "see_feature",
        assessed_at: assessedAt,
        reason_code: "executor_feature_view",
        reason: "Open a workload to inspect recovery authority for its exact UID.",
      },
      {
        component: "krr_report", source: "live_krr", status: "historical",
        assessed_at: assessedAt, report_id: reportId,
        reason_code: "report_retained", reason: "A qualified report was retained.",
      },
    ],
  };
  const operations: components["schemas"]["OperationsView"] = {
    assessed_at: assessedAt, jobs: [], jobs_capped: false, job_read_limit: 100,
    oldest_runnable_age_seconds: null, capacity_deferred: [], capacity_deferred_has_more: false,
    quota_waiting: [], quota_waiting_has_more: false, run_sample_limit: 10,
  };
  const knowledge: components["schemas"]["KnowledgeIndexStatus"] = {
    active_generation: {
      id: reportId, captured_at: assessedAt, indexed_at: assessedAt, config_digest: "config",
      source_digest: "source", fingerprint: "fingerprint", document_count: 2, chunk_count: 5,
      chunker_version: "1", index_format_version: "1", ranking_config_version: "1", search_config_version: "1",
    },
    catalog_search_available: true, has_active_generation: true, last_attempt: null, last_success: null,
    last_checked_at: assessedAt, last_checked_source_digest: "source", last_success_at: assessedAt,
    observed_at: assessedAt, read_available: true, retrieval_available: true,
    retrieval_mode: "catalog_full_text", semantic_mode: "not_configured", state: "ready",
    retained: { chunks: 5, document_versions: 2, generations: 1 },
  };
  let refreshFails = false;
  get.mockImplementation(async (path: string) => {
    if (path === "/api/health") {
      if (refreshFails) throw new Error("Health refresh failed");
      return { data: health };
    }
    if (path === "/api/connections/operations") return { data: operations };
    if (path === "/api/knowledge/index-status") {
      if (refreshFails) throw new Error("Knowledge refresh failed");
      return { data: knowledge };
    }
    if (["/api/connection-checks", "/api/connection-checks/status"].includes(path)) return { data: [] };
    throw new Error(`Unexpected request: ${path}`);
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={cache}>
      <Connections canMutate={false} actorId={workloadId} />
    </QueryClientProvider>,
  );
  try {
    const worker = (await screen.findByRole("heading", { name: "Worker" })).closest("article")!;
    expect(within(worker).getByText("never observed")).toBeTruthy();
    expect(within(worker).getByText("Observed: Not observed")).toBeTruthy();
    expect(screen.getByText("Collection dispatch: paused · class denied")).toBeTruthy();
    const kubernetes = screen.getByRole("heading", { name: "Kubernetes observation" }).closest("article")!;
    expect(within(kubernetes).getByText("denied")).toBeTruthy();
    expect(within(kubernetes).getByText(/Last success:/)).toBeTruthy();
    expect(within(kubernetes).getByText(/Last failure:/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open observed workload" }).getAttribute("href"))
      .toBe(`#/workloads?workload=${workloadId}`);
    expect(screen.getByRole("link", { name: "Open Workloads" }).getAttribute("href")).toBe("#/workloads");
    expect(screen.getByRole("link", { name: "Open retained resource report" }).getAttribute("href"))
      .toBe(`#/reports?report=${reportId}`);
    expect(await screen.findByText("No Runs are waiting for collection capacity.")).toBeTruthy();
    expect(await screen.findByText("2 document versions · 5 chunks")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Check .* connection/ })).toBeNull();
    expect(get.mock.calls.map(([path]) => path).sort()).toEqual([
      "/api/connection-checks", "/api/connection-checks/status", "/api/connections/operations", "/api/health", "/api/knowledge/index-status",
    ]);
    refreshFails = true;
    await act(() => Promise.all([
      cache.refetchQueries({ queryKey: ["health"] }),
      cache.refetchQueries({ queryKey: ["knowledge", "status"] }),
    ]));
    expect(await screen.findByText("Showing the last saved assessment.")).toBeTruthy();
    expect(screen.getByText("No Runs are waiting for collection capacity.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open retained resource report" })).toBeTruthy();
    expect(await screen.findByText("Error: Knowledge refresh failed")).toBeTruthy();
    expect(screen.getByText("2 document versions · 5 chunks")).toBeTruthy();
  } finally {
    cleanup();
    cache.clear();
  }
});
