// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Workloads } from "./Workloads";
import { WorkloadIdentity } from "./WorkloadIdentity";
import { workloadFixture } from "./fixtures";

const fixtureHttp = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>() }));
vi.mock("@/lib/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api: original.createTransport({ baseUrl: "http://localhost", fetch: fixtureHttp.fetch }).client };
});
const caches: QueryClient[] = [];
function mount(canMutate = false, onInvestigate = vi.fn()) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  render(<QueryClientProvider client={cache}><Workloads canMutate={canMutate} onInvestigate={onInvestigate} /></QueryClientProvider>);
}
beforeEach(() => {
  fixtureHttp.fetch.mockReset();
  fixtureHttp.fetch.mockImplementation(async request => {
    expect(new URL((request as Request).url).pathname).toBe("/api/workloads");
    expect((request as Request).method).toBe("GET");
    return Response.json(workloadFixture);
  });
});
afterEach(() => { cleanup(); caches.splice(0).forEach(cache => cache.clear()); });

describe("feature-local workload identity consumer", () => {
  it("reads generated-contract fields through the shared query and typed HTTP fixture for a Viewer", async () => {
    mount();
    await screen.findByRole("heading", { name: "demo/checkout" });
    await userEvent.setup().click(screen.getByText("Inspect workload identity"));
    expect(screen.getByText(workloadFixture.items[0].id)).toBeTruthy();
    expect(screen.getByText(workloadFixture.items[0].uid)).toBeTruthy();
    expect(screen.getByText("Unavailable", { exact: true })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Investigate workload" })).toBeNull();
    expect(fixtureHttp.fetch).toHaveBeenCalledTimes(1);
  });
  it("keeps inspection read-only while retaining the existing Operator investigation action", async () => {
    const investigate = vi.fn();
    mount(true, investigate);
    await screen.findByRole("heading", { name: "demo/checkout" });
    const user = userEvent.setup();
    await user.click(screen.getByText("Inspect workload identity"));
    expect(investigate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Investigate workload" }));
    expect(investigate).toHaveBeenCalledWith(workloadFixture.items[0].id);
    expect(fixtureHttp.fetch).toHaveBeenCalledTimes(1);
  });
  it("shows the API failure and retries the same consumer without inventing identity data", async () => {
    fixtureHttp.fetch.mockResolvedValueOnce(Response.json({ error: { code: "database_unavailable", message: "Application database unavailable." } }, { status: 503 }));
    mount();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(workloadFixture.items[0].uid)).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("heading", { name: "demo/checkout" });
    expect(fixtureHttp.fetch).toHaveBeenCalledTimes(2);
  });
  it("preserves the original identity and removed-scope explanation in historical consumers", async () => {
    render(<WorkloadIdentity workload={{ ...workloadFixture.items[0], active: false }} />);
    await userEvent.setup().click(screen.getByText("Inspect workload identity"));
    const details = screen.getByText("Inspect workload identity").closest("details")!;
    expect(within(details).getByText("Removed from configured scope")).toBeTruthy();
    expect(within(details).getByText(workloadFixture.items[0].uid)).toBeTruthy();
    expect(within(details).queryByRole("button")).toBeNull();
  });
});
