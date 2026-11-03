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

type WorkQueueItem = components["schemas"]["WorkQueueItem"];
const incident = (overrides: Partial<WorkQueueItem> = {}): WorkQueueItem => ({
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
  created_by_name: "operator",
  acknowledged_by: null,
  acknowledged_by_name: null,
  acknowledged_at: null,
  latest_run: null,
  run_blockers: [],
  relevant_executions: [],
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

function respond(items: WorkQueueItem[], next: number | null = null) {
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
  it("reveals new arrivals only after explicitly refreshing the anchored queue", async () => {
    const existing = incident({ title: "Existing incident" });
    const newer = incident({
      id: "44444444-4444-4444-8444-444444444444",
      title: "Newly arrived incident",
      created_at: "2026-11-01T00:00:01Z",
    });
    let arrived = false;
    mocks.get.mockImplementation(async (_path, options) => {
      const olderSnapshot = options.params.query.anchor_created_at === existing.created_at;
      const items = arrived && !olderSnapshot ? [newer, existing] : [existing];
      return {
        data: {
          items,
          next_offset: null,
          creation_anchor: { created_at: items[0].created_at, id: items[0].id },
        },
        response: new Response(null, { status: 200 }),
      };
    });
    mount();
    await screen.findByText(existing.title);
    await vi.waitFor(() => expect(window.location.hash).toContain("anchor_id="));
    arrived = true;
    expect(screen.queryByText(newer.title)).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh new work" }));
    expect(await screen.findByText(newer.title)).toBeTruthy();
    expect(screen.getByText(existing.title)).toBeTruthy();
  });

  it("parses the search term from the queue hash on mount", async () => {
    respond([]);
    queue("#/queue?search=timeout");
    mount();

    const search = screen.getByPlaceholderText(
      "Search by title or ID...",
    ) as HTMLInputElement;
    expect(search.value).toBe("timeout");
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

  it("includes resolved work in all states and returns to current work without a request error", async () => {
    const current = incident({ title: "Current incident" });
    const resolved = incident({
      id: "44444444-4444-4444-8444-444444444444",
      title: "Resolved incident",
      state: "resolved",
    });
    mocks.get.mockImplementation(async (_path, options) => {
      const view = options.params.query.view;
      if (view !== undefined && view !== "all" && view !== "resolved")
        return {
          error: { error: { code: "validation_error", message: "Invalid view" } },
          response: new Response(null, { status: 422 }),
        };
      return {
        data: {
          items: view === "all" ? [current, resolved] : view === "resolved" ? [resolved] : [current],
          next_offset: null,
        },
        response: new Response(null, { status: 200 }),
      };
    });
    mount();
    await screen.findByText(current.title);
    expect(screen.queryByText(resolved.title)).toBeNull();
    const user = userEvent.setup();
    const picker = screen.getByLabelText("Operational view");
    await user.selectOptions(picker, "all");
    expect(await screen.findByText(resolved.title)).toBeTruthy();
    expect(screen.getByText(current.title)).toBeTruthy();
    await user.selectOptions(picker, "resolved");
    await vi.waitFor(() => expect(screen.queryByText(current.title)).toBeNull());
    await user.selectOptions(picker, "default");
    expect(await screen.findByText(current.title)).toBeTruthy();
    expect(screen.queryByText(resolved.title)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });


  it("sorts severity alphabetically ascending from the select", async () => {
    respond([incident()]);
    mount();
    await screen.findByText("Database connection timeout");

    const user = userEvent.setup();
    await user.selectOptions(
      screen.getByLabelText("Sort incidents"),
      "severity",
    );

    await vi.waitFor(() =>
      expect(window.location.hash).toBe(
        "#/queue?sort_by=severity&sort_order=asc",
      ),
    );
  });
});

describe("work queue results", () => {

  it("recovers from an empty later page without claiming the queue is empty", async () => {
    mocks.get.mockImplementation(async (_path, options) => ({
      data: { items: options.params.query.offset === 0 ? [incident()] : [], next_offset: null },
      response: new Response(null, { status: 200 }),
    }));
    queue("#/queue?search=database&offset=25");
    mount();
    expect(await screen.findByText("No incidents on this page")).toBeTruthy();
    expect(screen.queryByText("No matching incidents")).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Database connection timeout")).toBeTruthy();
    expect(window.location.hash).toBe("#/queue?search=database");
  });

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

    expect(await screen.findByText("No open or acknowledged incidents")).toBeTruthy();
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
    await screen.findByText("No open or acknowledged incidents");
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
