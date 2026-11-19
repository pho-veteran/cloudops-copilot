// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearRequestKeys } from "@/features/investigations/request";
import type { components, operations } from "@/lib/api/generated/schema";
import { SourceChecks } from "./SourceChecks";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  return {
    ...module,
    api: module.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client,
  };
});

const date = "2026-11-18T10:30:00Z";
const id = "171784ae-fe15-4336-a423-c484e1c86024";
const metrics: components["schemas"]["MetricsCheck"] = {
  query: "memory_working_set",
  query_succeeded_at: date,
  coverage: "partial",
  coverage_reasons: ["stale"],
  latest_evaluation_at: date,
  raw_freshness: "unknown",
};
const status: components["schemas"]["CheckStatus"] = {
  component: "prometheus",
  source: "prometheus",
  status: "stale",
  reason_code: "metrics_stale",
  reason: "Scoped query points end before the current window.",
  assessed_at: date,
  target: { workload_id: id, uid: "test-uid", cluster: "lab", namespace: "demo", name: "checkout" },
  config_id: "test-config",
  observed_at: date,
  last_attempt_at: date,
  last_success_at: null,
  last_failure_at: date,
  stale_after_seconds: 300,
  latest_check_id: id,
  metrics,
};

function mount(canMutate: boolean) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { gcTime: 0 } } });
  return render(<QueryClientProvider client={cache}><SourceChecks canMutate={canMutate} /></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  http.fetch.mockReset();
  clearRequestKeys();
});

describe("configured source checks", () => {
  it("lets Viewers inspect coverage and unknown raw freshness without starting a check", async () => {
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      expect(request.method).toBe("GET");
      return Response.json(request.url.endsWith("/status") ? [status] : []);
    });
    mount(false);
    expect(await screen.findByText("Raw sample freshness")).toBeTruthy();
    expect(screen.getByText("unknown")).toBeTruthy();
    expect(screen.getByText(/checkout · UID test-uid/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Check Prometheus connection" })).toBeNull();
    expect(http.fetch).toHaveBeenCalledTimes(2);
  });

  it("sends only the component and preserves a key for an explicit retry after a lost reply", async () => {
    const requests: Array<{ key: string | null; body: unknown }> = [];
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === "GET") return Response.json(request.url.endsWith("/status") ? [status] : []);
      requests.push({ key: request.headers.get("idempotency-key"), body: await request.json() });
      if (requests.length === 1) throw new TypeError("Lost reply");
      const receipt: operations["create_connection_check"]["responses"][201]["content"]["application/json"] = {
        id, component: "prometheus", status: "unavailable", source: "prometheus",
        reason: "The source check reached its deadline.", reason_code: "timeout",
        created_by: id, observed_at: date, historical: false,
      };
      return Response.json(receipt, { status: 201 });
    });
    mount(true);
    const user = userEvent.setup();
    const button = screen.getByRole("button", { name: "Check Prometheus connection" });
    await user.click(button);
    await screen.findByText(/Cannot reach the local API/);
    expect(requests).toHaveLength(1);
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    await user.click(button);
    await screen.findByText("The source check reached its deadline.");
    expect(requests).toHaveLength(2);
    expect(requests[0].key).toMatch(/^[0-9a-f-]{36}$/);
    expect(requests[0].body).toEqual({ component: "prometheus" });
    expect(requests[1]).toEqual(requests[0]);
  });
});
