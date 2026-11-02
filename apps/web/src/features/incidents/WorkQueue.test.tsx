// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { WorkQueue } from "./WorkQueue";

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api: { ...original.api, GET: mocks.get } };
});

type IncidentView = components["schemas"]["IncidentView"];
const incident = (overrides: Partial<IncidentView> = {}): IncidentView => ({
  id: "11111111-1111-4111-8111-111111111111",
  title: "Database connection timeout",
  description: "",
  state: "open",
  severity: { value: "critical", provenance: "operator" },
  origin: { kind: "operator", source_id: null },
  alert_signal: "none",
  workload_id: "22222222-2222-4222-8222-222222222222",
  created_by: "33333333-3333-4333-8333-333333333333",
  created_at: "2026-11-01T00:00:00Z",
  first_seen_at: null,
  last_seen_at: null,
  ...overrides,
});

const caches: QueryClient[] = [];

function queue(hash: string) {
  window.history.replaceState(null, "", `/${hash}`);
}

function mount() {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  caches.push(cache);
  render(
    <QueryClientProvider client={cache}>
      <WorkQueue />
    </QueryClientProvider>,
  );
  return cache;
}

function respond(items: IncidentView[], next: number | null = null) {
  mocks.get.mockResolvedValue({
    data: { items, next_offset: next },
    response: new Response(null, { status: 200 }),
  });
}

beforeEach(() => {
  mocks.get.mockReset();
  queue("#/queue");
});

afterEach(() => {
  cleanup();
  caches.splice(0).forEach((cache) => cache.clear());
});

describe("work queue URL state", () => {
  it("parses the search term from the queue hash on mount", async () => {
    respond([]);
    queue("#/queue?search=timeout");
    mount();

    const search = screen.getByPlaceholderText(
      "Search by title or ID...",
    ) as HTMLInputElement;
    expect(search.value).toBe("timeout");
    await vi.waitFor(() => expect(mocks.get).toHaveBeenCalled());
    expect(mocks.get.mock.calls[0][1].params.query.search).toBe("timeout");
  });

  it("writes typed search to the hash and resets the offset", async () => {
    respond([incident()]);
    queue("#/queue?offset=25");
    mount();
    await screen.findByText("Database connection timeout");

    const user = userEvent.setup();
    await user.type(
      screen.getByPlaceholderText("Search by title or ID..."),
      "db",
    );

    await vi.waitFor(() =>
      expect(window.location.hash).toBe("#/queue?search=db"),
    );
  });

  it("resets the offset when a filter changes", async () => {
    respond([incident()]);
    queue("#/queue?offset=25");
    mount();
    await screen.findByText("Database connection timeout");

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Filter by state"), "open");

    await vi.waitFor(() =>
      expect(window.location.hash).toBe("#/queue?state=open"),
    );
  });
});

describe("work queue results", () => {
  it("shows the no-match panel when a search returns nothing", async () => {
    respond([]);
    queue("#/queue?search=missing");
    mount();

    expect(await screen.findByText("No matching incidents")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  });

  it("shows the no-match panel when filters exclude every incident", async () => {
    respond([]);
    queue("#/queue?state=resolved&severity=critical");
    mount();

    expect(await screen.findByText("No matching incidents")).toBeTruthy();
    expect(window.location.hash).toContain("state=resolved");
  });

  it("shows the empty queue prompt when no filter is active", async () => {
    respond([]);
    mount();

    expect(await screen.findByText("No incidents yet")).toBeTruthy();
  });

  it("renders the failure state with a retry action", async () => {
    mocks.get.mockResolvedValue({
      error: {
        error: { code: "workload_not_found", message: "Unknown workload" },
      },
      response: new Response(null, { status: 404 }),
    });
    queue("#/queue?workload=74483c3e-adc2-4d05-93a6-c218afc6ebe7");
    mount();

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/Unknown workload/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("highlights the incident recorded as selected", async () => {
    const item = incident();
    respond([item]);
    queue(`#/queue?selected=${item.id}`);
    mount();

    const card = await screen.findByRole("link", {
      name: /Database connection timeout/,
    });
    expect(card.className).toContain("selected");
  });

  it("never sends a workload filter that is not a UUID", async () => {
    respond([]);
    mount();
    await screen.findByText("No incidents yet");
    const requests = mocks.get.mock.calls.length;

    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("Filter by workload UUID"),
      "not-a-uuid",
    );

    expect(mocks.get.mock.calls.length).toBe(requests);
    for (const call of mocks.get.mock.calls)
      expect(call[1].params.query.workload_id).toBeUndefined();
    expect(window.location.hash).toBe("#/queue");
  });
});
