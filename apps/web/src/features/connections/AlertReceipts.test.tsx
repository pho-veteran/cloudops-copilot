// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { AlertReceipts } from "./AlertReceipts";
import {
  alertReceiptFixture,
  alertReceiptsPageFixture,
  capacityIncidentId as incident,
  capacityRunId as run,
} from "./fixtures";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...original,
    api: original.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client,
  };
});

type Receipt = components["schemas"]["AlertReceiptView"];
const receipt = (overrides: Partial<Receipt> = {}) => alertReceiptFixture({
  outcome: "accepted",
  ...overrides,
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
      <AlertReceipts />
    </QueryClientProvider>,
  );
}
const page = (items: Receipt[], next_cursor: string | null = null, configured = true) =>
  Response.json(alertReceiptsPageFixture({
    configured,
    source_id: configured ? "lab-alertmanager" : null,
    items,
    next_cursor,
  }));

it("shows each outcome with a readable reason and the exact incident link", async () => {
  http.fetch.mockResolvedValue(
    page([
      receipt(),
      receipt({ outcome: "deferred" }),
      receipt({
        outcome: "rejected",
        reason: "out_of_scope",
        incident_id: null,
        run_id: null,
        target_name: "ledger",
      }),
      receipt({ outcome: "ignored", alert_status: "resolved", reason: "resolved_before_admission", incident_id: null, run_id: null }),
      receipt({ outcome: "retryable", reason: "capacity_exhausted", incident_id: null, run_id: null }),
      receipt({ outcome: "resolved_signal", alert_status: "resolved", run_id: null }),
      receipt({ outcome: "duplicate", reason: "future_code", incident_id: null, run_id: null }),
    ]),
  );
  mount();
  expect(await screen.findByText("Intake configured · source lab-alertmanager")).toBeTruthy();
  for (const label of ["Accepted", "Accepted · deferred at delivery", "Rejected", "Ignored", "Retry requested at delivery", "Alert resolved", "Duplicate"])
    expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText(/Check the alert's cluster, namespace and workload/)).toBeTruthy();
  expect(screen.getByText("Investigation capacity was full.")).toBeTruthy();
  expect(screen.getByText(/Reason unavailable/)).toBeTruthy();
  const runLinks = screen.getAllByRole("link", { name: "Open Run" });
  expect(runLinks).toHaveLength(2);
  expect(runLinks[0].getAttribute("href")).toBe(`#/incidents/${incident}?run=${run}`);
  expect(screen.getByRole("link", { name: "Open incident" }).getAttribute("href")).toBe(`#/incidents/${incident}`);
  expect(screen.getByText("Rejected").closest("li")?.querySelector("a")).toBeNull();
  // A retained retry request for the same fingerprint stays historical after admission.
  expect(screen.getByText("Retry requested at delivery").closest("li")?.querySelector("a")).toBeNull();
  expect(screen.queryByText(/Retry pending|will retry/i)).toBeNull();
  expect(screen.queryByRole("button", { name: /Check|Delete|Retry delivery/ })).toBeNull();
});

it("distinguishes unconfigured intake, empty history and a failed read with retry", async () => {
  http.fetch.mockResolvedValueOnce(page([], null, false));
  mount();
  expect(await screen.findByText(/Alert intake is not configured/)).toBeTruthy();
  expect(screen.getByText("No retained deliveries.")).toBeTruthy();
  cleanup();
  http.fetch.mockReset();
  cache.clear();
  http.fetch
    .mockResolvedValueOnce(
      Response.json({ error: { code: "database_unavailable", message: "Application database unavailable." } }, { status: 503 }),
    )
    .mockResolvedValueOnce(page([receipt()]));
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findByText("Accepted")).toBeTruthy();
});

it("loads older receipts with the returned cursor and keeps them after a failed page", async () => {
  let failOlder = true;
  http.fetch.mockImplementation(async (request) => {
    const url = new URL((request as Request).url);
    expect((request as Request).method).toBe("GET");
    if (url.searchParams.get("cursor") === "older") {
      if (failOlder) {
        failOlder = false;
        return Response.json({ error: { code: "database_unavailable", message: "Database unavailable." } }, { status: 503 });
      }
      return page([receipt({ alertname: "OlderAlert" })]);
    }
    return page([receipt()], "older");
  });
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Load older receipts" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getByText("CheckoutDown")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByText("OlderAlert")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Load older receipts" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});
