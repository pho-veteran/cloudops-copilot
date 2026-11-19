// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { safeRoute } from "@/app/routes";
import {
  capacityIncidentId,
  capacityRunId,
  operationsFixture,
  quotaIncidentId,
  quotaRunId,
} from "./fixtures";
import { OperationsPanel } from "./OperationsPanel";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...original,
    api: original.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client,
  };
});

let cache: QueryClient;
beforeEach(() => {
  http.fetch.mockReset();
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  cleanup();
  cache.clear();
});

function mount() {
  render(
    <QueryClientProvider client={cache}>
      <OperationsPanel />
    </QueryClientProvider>,
  );
}

it("keeps queue and permit signals distinct and links the exact capacity and quota Runs", async () => {
  http.fetch.mockResolvedValueOnce(Response.json(
    operationsFixture({ run_sample_limit: 1, capacity_deferred_has_more: true }),
  ));
  mount();
  expect(await screen.findByText("Dispatch paused")).toBeTruthy();
  expect(screen.getByText(/paused this job kind for host recovery/)).toBeTruthy();
  expect(screen.getByText("Runnable 2 · Due retry 1 · Future retry 1 · Expired lease 1")).toBeTruthy();
  expect(screen.getByText("Oldest runnable job age 1m")).toBeTruthy();
  const capacity = screen.getByRole("region", { name: "Waiting for collection capacity" });
  const quota = screen.getByRole("region", { name: "Waiting for diagnosis quota" });
  expect(within(capacity).getByText(/More Runs are waiting/)).toBeTruthy();
  expect(within(quota).getByText(/Review resume eligibility in the Run/)).toBeTruthy();
  expect(safeRoute(within(capacity).getByRole("link", { name: "Checkout latency" }).getAttribute("href")!))
    .toMatchObject({ page: "incidents", id: capacityIncidentId, run: capacityRunId });
  expect(safeRoute(within(quota).getByRole("link", { name: "Ledger startup" }).getAttribute("href")!))
    .toMatchObject({ page: "incidents", id: quotaIncidentId, run: quotaRunId });
  expect(screen.queryByRole("button", { name: /resume|dispatch/i })).toBeNull();
});

it("labels capped counts and keeps the last snapshot when a read fails", async () => {
  const snapshot = operationsFixture();
  const capped = {
    ...snapshot,
    jobs: snapshot.jobs.map((job) => ({ ...job, counts: null })),
    jobs_capped: true,
    oldest_runnable_age_seconds: null,
    capacity_deferred: [],
    capacity_deferred_has_more: false,
    quota_waiting: [],
  };
  const unavailable = () => Response.json(
    { error: { code: "database_unavailable", message: "Application database unavailable." } },
    { status: 503 },
  );
  http.fetch
    .mockResolvedValueOnce(unavailable())
    .mockResolvedValueOnce(Response.json(capped))
    .mockResolvedValueOnce(unavailable())
    .mockResolvedValueOnce(Response.json(snapshot));
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findAllByText("Job counts unknown.")).toHaveLength(snapshot.jobs.length);
  expect(screen.getByText(/more than 1000 active jobs/i)).toBeTruthy();
  expect(screen.getByText("No Runs are waiting for collection capacity.")).toBeTruthy();
  expect(screen.getByText("No Runs are waiting for diagnosis quota.")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Refresh operations" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getAllByText("Job counts unknown.")).toHaveLength(snapshot.jobs.length);
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("link", { name: "Checkout latency" })).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(http.fetch.mock.calls.map(([request]) => (request as Request).method)).toEqual(
    Array(4).fill("GET"),
  );
});
