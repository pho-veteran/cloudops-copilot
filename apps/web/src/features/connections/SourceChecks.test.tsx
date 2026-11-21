// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
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
  const view = render(<QueryClientProvider client={cache}><SourceChecks canMutate={canMutate} actorId={id} /></QueryClientProvider>);
  return { ...view, cache };
}

function completedCheck() {
  const receipt: operations["create_connection_check"]["responses"][201]["content"]["application/json"] = {
    id, component: "prometheus", status: "unavailable", source: "prometheus",
    reason: "The source check reached its deadline.", reason_code: "timeout",
    created_by: id, observed_at: date, historical: false,
  };
  return Response.json(receipt, { status: 201 });
}

afterEach(() => {
  cleanup();
  http.fetch.mockReset();
  clearRequestKeys();
  sessionStorage.clear();
  onlineManager.setOnline(true);
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
      return completedCheck();
    });
    const first = mount(true);
    const user = userEvent.setup();
    const check = () => screen.getByRole("button", { name: "Check Prometheus connection" });
    await user.click(check());
    await screen.findByText(/Cannot reach the local API/);
    expect(requests).toHaveLength(1);
    first.unmount();
    clearRequestKeys();
    mount(true);
    await screen.findByText("Raw sample freshness");
    expect(requests).toHaveLength(1);
    await user.click(check());
    await screen.findByText("The source check reached its deadline.");
    expect(requests).toHaveLength(2);
    expect(requests[0].key).toMatch(/^[0-9a-f-]{36}$/);
    expect(requests[0].body).toEqual({ component: "prometheus" });
    expect(requests[1]).toEqual(requests[0]);
  });

  it("ages a retained check when refresh fails and the wall clock jumps", async () => {
    const recent = {
      ...status, status: "available", reason_code: "query_succeeded", reason: "Scoped data returned.",
      observed_at: new Date(Date.parse(date) - 299_000).toISOString(),
    };
    let fail = false;
    http.fetch.mockImplementation(async (input) => {
      if (fail && (input as Request).url.endsWith("/status")) throw new TypeError("Offline");
      return Response.json((input as Request).url.endsWith("/status") ? [recent] : []);
    });
    const { cache } = mount(false);
    const card = (await screen.findByRole("heading", { name: "Prometheus check" })).closest("article")!;
    expect(await within(card).findByText("available", { exact: true })).toBeTruthy();
    fail = true;
    await act(() => cache.refetchQueries({ queryKey: ["connection-checks", "status"] }));
    const original = performance.now();
    const clock = vi.spyOn(performance, "now").mockReturnValue(original + 2500);
    const wall = vi.spyOn(Date, "now").mockReturnValue(Date.now() - 3600_000);
    try {
      expect(await within(card).findByText("stale", { exact: true }, { timeout: 2000 })).toBeTruthy();
      expect(within(card).getByText("unknown")).toBeTruthy();
      expect(screen.getByText(/Cannot reach the local API/)).toBeTruthy();
    } finally {
      clock.mockRestore();
      wall.mockRestore();
    }
  });

  it("does not queue a check to send after reconnect", async () => {
    const posts: string[] = [];
    const user = userEvent.setup();
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === "GET") return Response.json(request.url.endsWith("/status") ? [status] : []);
      if (!onlineManager.isOnline()) throw new TypeError("Offline");
      posts.push(request.url);
      return completedCheck();
    });
    mount(true);
    await screen.findByText("Raw sample freshness");
    onlineManager.setOnline(false);
    await user.click(screen.getByRole("button", { name: "Check Prometheus connection" }));
    await screen.findByText(/Cannot reach the local API/);
    onlineManager.setOnline(true);
    await waitFor(() => expect(http.fetch.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(posts).toHaveLength(0);
  });

  it("keeps the new attempt's retry key when the original response arrives late", async () => {
    let finishOriginal!: (response: Response) => void;
    let loseNewReply!: (error: Error) => void;
    const originalReply = new Promise<Response>((resolve) => { finishOriginal = resolve; });
    const newReply = new Promise<Response>((_, reject) => { loseNewReply = reject; });
    const keys: Array<string | null> = [];
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === "GET") return Response.json(request.url.endsWith("/status") ? [status] : []);
      keys.push(request.headers.get("idempotency-key"));
      if (keys.length === 1) return originalReply;
      if (keys.length === 3) return newReply;
      return completedCheck();
    });
    const user = userEvent.setup();
    const button = () => screen.getByRole("button", { name: "Check Prometheus connection" });
    const original = mount(true);
    await user.click(button());
    await waitFor(() => expect(keys).toHaveLength(1));
    original.unmount();
    mount(true);
    await user.click(button());
    await screen.findByText("The source check reached its deadline.");
    await user.click(button());
    await waitFor(() => expect(keys).toHaveLength(3));

    finishOriginal(completedCheck());
    await waitFor(() => expect(original.cache.isMutating()).toBe(0));
    loseNewReply(new TypeError("Lost reply"));
    await screen.findByText(/Cannot reach the local API/);
    await user.click(button());
    await screen.findByText("The source check reached its deadline.");

    expect(keys).toHaveLength(4);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
    expect(keys[3]).toBe(keys[2]);
  });
});
