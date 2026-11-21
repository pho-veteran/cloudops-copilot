// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { components } from "@/lib/api/generated/schema";
import { Connections } from "./Connections";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { GET: get },
  unwrap: ({ data }: { data: unknown }) => data,
}));
vi.mock("./AlertReceipts", () => ({ AlertReceipts: () => null }));
vi.mock("@/features/knowledge/KnowledgeStatus", () => ({ KnowledgeStatus: () => null }));
vi.mock("@/features/diagnosis/ModelConnection", () => ({ ModelConnection: () => null }));

it("shows unobserved and denied states and links to workload authority without polling it", async () => {
  const assessedAt = "2026-11-18T01:02:03Z";
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
    ],
  };
  get.mockImplementation(async (path: string) => {
    if (path === "/api/health") return { data: health };
    if (["/api/connection-checks", "/api/connection-checks/status"].includes(path)) return { data: [] };
    throw new Error(`Unexpected request: ${path}`);
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={cache}>
      <Connections canMutate={false} />
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
    expect(get.mock.calls.map(([path]) => path).sort()).toEqual([
      "/api/connection-checks", "/api/connection-checks/status", "/api/health",
    ]);
  } finally {
    cleanup();
    cache.clear();
  }
});
