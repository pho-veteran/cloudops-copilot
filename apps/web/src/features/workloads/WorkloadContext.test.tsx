// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { WorkloadContext } from "./WorkloadContext";

const http = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  return { ...module, api: { ...module.api, GET: http.get } };
});
vi.mock("./queries", () => ({
  useObservation: () => ({ isPending: true }),
  useObservationHistory: () => ({ isPending: true }),
}));
vi.mock("./WorkloadHealth", () => ({
  useAgedObservation: () => ({ health: "unknown" }),
  WorkloadHealth: () => null,
}));

const caches: QueryClient[] = [];
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((cache) => cache.clear());
  http.get.mockReset();
});

describe("related incidents", () => {
  it("includes resolved history for the selected workload", async () => {
    const workload = "22222222-2222-4222-8222-222222222222";
    const resolved: components["schemas"]["WorkQueueItem"] = {
      id: "11111111-1111-4111-8111-111111111111",
      workload_id: workload,
      title: "Resolved history",
      description: "",
      state: "resolved",
      created_at: "2026-11-01T00:00:00Z",
      created_by: "33333333-3333-4333-8333-333333333333",
      origin: { kind: "operator", source_id: null },
      severity: { value: null, provenance: "not_provided" },
      alert_signal: "none",
      first_seen_at: null,
      last_seen_at: null,
      created_by_name: "operator",
      acknowledged_by: null,
      acknowledged_by_name: null,
      acknowledged_at: null,
      latest_run: null,
      run_blockers: [],
      relevant_executions: [],
    };
    http.get.mockImplementation(async (_path, options) => ({
      data: {
        items: options.params.query.view === "all"
          ? [resolved]
          : [],
        next_offset: null,
      },
      response: new Response(null, { status: 200 }),
    }));
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    caches.push(cache);
    render(<QueryClientProvider client={cache}><WorkloadContext id={workload} /></QueryClientProvider>);
    expect(await screen.findByText(/Resolved history/)).toBeTruthy();
  });
});
