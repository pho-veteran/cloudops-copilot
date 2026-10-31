// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { ScopeStatus } from "./ScopeStatus";
import { workloadFixture } from "./fixtures";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>() }));
vi.mock("@/lib/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api: original.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client };
});
const target: components["schemas"]["ScopeTargetView"] = {
  id: "00000000-0000-4000-8000-000000000002", cluster: "lab", namespace: "demo", name: "unresolved",
  configured: true, workload_id: null, uid: null, state: "unavailable", reason: "permission_denied",
  last_attempt_at: "2026-10-26T00:00:00Z", last_success_at: null,
};
let cache: QueryClient;
beforeEach(() => { http.fetch.mockReset(); cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); });
afterEach(() => { cleanup(); cache.clear(); });
function mount() { render(<QueryClientProvider client={cache}><ScopeStatus /></QueryClientProvider>); }

it("shows unresolved scope without inventing a workload ID or mutation controls", async () => {
  http.fetch.mockResolvedValue(Response.json({ items: [target], next_offset: null }));
  mount();
  await screen.findByText("demo/unresolved");
  expect(screen.getByText("The scoped reader could not access this Deployment.")).toBeTruthy();
  expect(screen.getByText(/No saved identity/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Investigate|baseline|Inspect saved/ })).toBeNull();
  expect(screen.getByText(/does not establish workload health/)).toBeTruthy();
});

it("reads the exact saved historical identity and truthful source after an explicit inspection", async () => {
  http.fetch.mockImplementation(async request => {
    const path = new URL((request as Request).url).pathname;
    expect((request as Request).method).toBe("GET");
    if (path === "/api/workloads/scope") return Response.json({ items: [{ ...target, state: "missing", reason: "not_found", workload_id: workloadFixture.items[0].id, uid: "original-live-uid" }], next_offset: null });
    expect(path).toBe(`/api/workloads/${workloadFixture.items[0].id}`);
    return Response.json({ ...workloadFixture.items[0], source: "live_kubernetes", uid: "original-live-uid", active: false, retirement_reason: "deleted" });
  });
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Inspect saved identity" }));
  await user.click(await screen.findByText("Inspect workload identity"));
  expect(screen.getByText("Deployment deleted")).toBeTruthy();
  expect(screen.getByText("Live Kubernetes identity")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Confirm|Investigate/ })).toBeNull();
});

it("keeps removed-target browsing and pagination as explicit protected reads", async () => {
  http.fetch.mockImplementation(async request => {
    const url = new URL((request as Request).url);
    if (url.searchParams.get("removed") === "true") return Response.json({ items: [{ ...target, configured: false, state: "removed", reason: null }], next_offset: null });
    return Response.json({ items: [], next_offset: 25 });
  });
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Next scope page" }));
  await vi.waitFor(() => expect(http.fetch.mock.calls.some(([r]) => new URL((r as Request).url).searchParams.get("offset") === "25")).toBe(true));
  await user.click(screen.getByRole("button", { name: "Show removed targets" }));
  await screen.findByText("Removed from scope");
  const last = new URL((http.fetch.mock.calls.at(-1)![0] as Request).url);
  expect(last.searchParams.get("offset")).toBe("0");
  expect(last.searchParams.get("removed")).toBe("true");
});

it("recovers from an API failure using an explicit retry", async () => {
  http.fetch.mockResolvedValueOnce(Response.json({ error: { code: "database_unavailable", message: "Status unavailable." } }, { status: 503 }))
    .mockResolvedValue(Response.json({ items: [], next_offset: null }));
  mount();
  await screen.findByRole("alert");
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText(/No versioned live scope records/);
});
