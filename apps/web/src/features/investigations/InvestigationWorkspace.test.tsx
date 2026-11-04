/// <reference lib="es2024.promise" />
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { workloadFixture } from "@/features/workloads/fixtures";
import { clearRequestKeys } from "./request";
import { InvestigationWorkspace } from "./InvestigationWorkspace";
import { IncidentActivity } from "@/features/incidents/IncidentActivity";
import { Workspace } from "@/app/FoundationApp";
import { parseWorkQueueHash, readRoute } from "@/app/routes";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  return {
    ...module,
    api: module.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client,
  };
});
const caches: QueryClient[] = [];
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((c) => c.clear());
  http.fetch.mockReset();
  clearRequestKeys();
  window.location.hash = "";
});

const incidentId = "00000000-0000-4000-8000-000000000010";
const workload = { ...workloadFixture.items[0], observation: undefined };
function run(overrides: Partial<components["schemas"]["RunView"]> = {}) {
  return {
    id: "00000000-0000-4000-8000-000000000020",
    incident_id: incidentId,
    state: "insufficient_evidence",
    reason: "Fixture collection saved.",
    created_at: "2026-11-01T00:00:00Z",
    finished_at: "2026-11-01T00:01:00Z",
    requested_by: "00000000-0000-4000-8000-000000000030",
    origin: { kind: "operator", source_id: null },
    question: "Why did checkout restart?",
    question_source: "operator",
    window: {
      start: "2026-10-31T23:30:00Z",
      end: "2026-11-01T00:00:00Z",
      source: "preset_30",
    },
    queue_reason: null,
    outcome_code: null,
    evidence: [],
    attempts: [],
    diagnosis: "unavailable",
    recovery: "unavailable",
    ...overrides,
  } satisfies components["schemas"]["RunView"];
}

function mount(
  runs: components["schemas"]["RunView"][],
  state = "open",
  incident: Record<string, unknown> = {},
  fullWorkspace = false,
  override?: (request: Request) => Response | undefined,
) {
  const posts: { path: string; key: string | null; body: unknown }[] = [];
  http.fetch.mockImplementation(async (input) => {
    const request = input as Request;
    const overridden = override?.(request);
    if (overridden) return overridden;
    const path = new URL(request.url).pathname;
    if (path === "/api/session") return Response.json({
      principal: { id: "00000000-0000-4000-8000-000000000030", username: "operator", role: "operator" },
      csrf_token: "test-csrf",
      expires_at: "2099-01-01T00:00:00Z",
    });
    if (request.method === "POST") {
      const body = await request.clone().json().catch(() => null);
      posts.push({ path, key: request.headers.get("idempotency-key"), body });
      if (path.endsWith("/cancel"))
        return Response.json({ ...runs[0], state: "cancelled", queue_reason: null });
      return Response.json(
        { incident_id: incidentId, run_id: "00000000-0000-4000-8000-000000000021", state: "queued", queue_reason: null, window: null },
        { status: 201 },
      );
    }
    if (path === "/api/incidents") return Response.json({
      items: [{
        id: incidentId, title: "Checkout restarts", description: "", state: "open",
        severity: { value: null, provenance: "not_provided" },
        origin: { kind: "alertmanager", source_id: "lab-alertmanager" },
        alert_signal: "none", workload_id: workload.id, created_by: null,
        created_at: "2026-11-01T00:00:00.123456Z", first_seen_at: null,
        last_seen_at: null, created_by_name: null, acknowledged_by: null,
        acknowledged_by_name: null, acknowledged_at: null, latest_run: null,
        run_blockers: [], relevant_executions: [],
      }], next_offset: null,
    });
    if (path.endsWith("/runs")) return Response.json({ items: runs, next_offset: null });
    if (path.includes("/runs/")) return Response.json(runs.find((r) => path.endsWith(r.id)));
    if (path.endsWith("/activity")) return Response.json({ items: [], total: 0, next_cursor: null });
    if (path.endsWith("/handoff/preview")) return Response.json({ incident_id: incidentId, resolution_id: null, schema_version: "HandoffSnapshot/v1", snapshot: {}, markdown: "Current handoff" });
    if (path.endsWith("/plans") || path.endsWith("/executions")) return Response.json([]);
    return Response.json({
      id: incidentId,
      workload_id: workload.id,
      title: "Checkout restarts",
      description: "Giữ nguyên mô tả.",
      state,
      created_by: null,
      created_at: "2026-11-01T00:00:00Z",
      origin: { kind: "alertmanager", source_id: "lab-alertmanager" },
      first_seen_at: null,
      last_seen_at: null,
      severity: { value: null, provenance: "not_provided" },
      alert_signal: "none",
      workload,
      ...incident,
    });
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  render(
    <QueryClientProvider client={cache}>
      {fullWorkspace ? <Workspace /> : <InvestigationWorkspace id={incidentId} selectedRun="" selectedResolution="" canMutate />}
    </QueryClientProvider>,
  );
  return posts;
}

describe("investigation requests", () => {
  it("returns from a queue card through the in-app link with its full context", async () => {
    const anchor = "2026-11-01T00:00:00.123456Z";
    window.history.replaceState(null, "", `/#/queue?search=Checkout&sort_by=created_at&offset=25&anchor_created_at=${encodeURIComponent(anchor)}&anchor_id=${incidentId}`);
    mount([run()], "open", {}, true);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: /Checkout restarts/ }));
    await vi.waitFor(() => expect(readRoute().page).toBe("incidents"));
    await user.click(await screen.findByRole("link", { name: new RegExp(run().id) }));
    await vi.waitFor(() => expect(readRoute().run).toBe(run().id));
    const back = await screen.findByRole("link", { name: "← Work queue" });
    await user.click(back);
    await vi.waitFor(() => expect(readRoute().page).toBe("queue"));
    expect(parseWorkQueueHash(window.location.hash)).toMatchObject({
      search: "Checkout", sort_by: "created_at", offset: 25, selected: incidentId,
      anchor_created_at: anchor, anchor_id: incidentId,
    });
    expect((await screen.findByRole("link", { name: /Checkout restarts/ })).className).toContain("selected");
    expect((screen.getByLabelText("Search incidents") as HTMLInputElement).value).toBe("Checkout");
  });

  it.each(["analyzing", "waiting_for_quota"] as const)("keeps %s work busy like the server guard", async (state) => {
    mount([run({ state, finished_at: null })]);
    expect(await screen.findByText(/An investigation is still active/)).toBeTruthy();
    expect(screen.queryByLabelText("Why is a new investigation needed?")).toBeNull();
  });

  it("reviews the reason and window before submitting the exact workload request", async () => {
    const posts = mount([run()]);
    const user = userEvent.setup();
    expect(await screen.findByText(/ALERTMANAGER · LAB-ALERTMANAGER/)).toBeTruthy();
    await user.type(
      await screen.findByLabelText("Why is a new investigation needed?"),
      "Restarts resumed after the deploy",
    );
    await user.selectOptions(screen.getByLabelText("Investigate"), "60");
    await user.click(screen.getByRole("button", { name: "Review request" }));
    const review = screen.getByRole("region", { name: "Review the investigation request" });
    expect(within(review).getByText(/Last 60 minutes/)).toBeTruthy();
    expect(posts).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).toEqual({
      workload_id: workload.id,
      reason: "Restarts resumed after the deploy",
      window: { preset: 60 },
    });
    expect(posts[0].key).toMatch(/^[0-9a-f-]{36}$/);
    await vi.waitFor(() =>
      expect(window.location.hash).toBe(
        `#/incidents/${incidentId}?run=00000000-0000-4000-8000-000000000021`,
      ),
    );
  });

  it("shows a deferred request with its saved window and cancels it after confirmation", async () => {
    const posts = mount([
      run({ state: "queued", queue_reason: "capacity", finished_at: null, question_source: "automatic" }),
    ]);
    const user = userEvent.setup();
    expect((await screen.findAllByText("waiting for capacity")).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Why is a new investigation needed?")).toBeNull();
    expect(await screen.findByText(/UTC\) · Last 30 minutes at admission/)).toBeTruthy();
    await user.click(await screen.findByRole("button", { name: "Cancel queued investigation" }));
    expect(posts).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Confirm cancellation" }));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].path).toBe(`/api/incidents/${incidentId}/runs/${run().id}/cancel`);
  });

  it("labels legacy requests honestly and asks to reopen a resolved incident", async () => {
    mount([run({ question: null, question_source: "not_captured", window: null })], "resolved");
    expect((await screen.findAllByText("Not captured for this earlier request")).length).toBe(2);
    expect(screen.getByText("Reopen this incident before requesting a new investigation.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review request" })).toBeNull();
  });

  it("shows a resolved alert signal as metadata while the incident stays open", async () => {
    mount([run()], "open", {
      alert_signal: "resolved",
      severity: { value: "critical", provenance: "alert_label" },
      first_seen_at: "2026-11-01T10:00:00Z",
      last_seen_at: "2026-11-01T10:05:00Z",
    });
    const metadata = await screen.findByLabelText("Alert signal");
    expect(within(metadata).getByText("Resolved")).toBeTruthy();
    expect(within(metadata).getByText(/incident lifecycle is unchanged/)).toBeTruthy();
    expect(within(metadata).getByText("critical (alert label)")).toBeTruthy();
    expect(within(metadata).queryByText("Not captured")).toBeNull();
    expect(screen.getByText(/INCIDENT · OPEN · ALERTMANAGER/)).toBeTruthy();
  });

  it("omits alert metadata for manual incidents and says when severity is absent", async () => {
    mount([run()], "open", { origin: { kind: "operator", source_id: null } });
    expect(await screen.findByText(/INCIDENT · OPEN · MANUAL/)).toBeTruthy();
    expect(screen.queryByLabelText("Alert signal")).toBeNull();
    cleanup();
    mount([run()], "open", { alert_signal: "firing" });
    const metadata = await screen.findByLabelText("Alert signal");
    expect(within(metadata).getByText("Not provided")).toBeTruthy();
    expect(within(metadata).getAllByText("Not captured")).toHaveLength(2);
  });
});

describe("persisted handoff navigation", () => {
  const resolution = "00000000-0000-4000-8000-000000000099";
  const human = "<img src=x onerror=alert(1)> Ghi chú & evidence";
  const previewResponse = (selected: string | null) => Response.json({
    incident_id: incidentId, resolution_id: selected, schema_version: "HandoffSnapshot/v1",
    snapshot: {}, markdown: selected ? `Saved: ${human}` : "Current handoff",
  });

  it("retains the selected resolution on reload and when navigating Runs", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}?resolution=${resolution}&return_to=${encodeURIComponent("#/queue?search=db")}`);
    const handler = (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname.endsWith("/handoff/preview")) return previewResponse(url.searchParams.get("resolution_id"));
    };
    mount([run()], "open", {}, true, handler);
    expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
    await userEvent.setup().click(await screen.findByRole("link", { name: new RegExp(run().id) }));
    await vi.waitFor(() => expect(readRoute()).toMatchObject({ run: run().id, resolution }));
    expect(parseWorkQueueHash(readRoute().page === "incidents" ? new URLSearchParams(window.location.hash.split("?")[1]).get("return_to")! : "")).toMatchObject({ search: "db" });
    cleanup();
    mount([run()], "open", {}, true, handler);
    expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
    expect(readRoute().resolution).toBe(resolution);
  });

  it("retains the saved selection when a new Run is requested", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}?resolution=${resolution}`);
    mount([run()], "open", {}, true, (request) => {
      const url = new URL(request.url);
      if (url.pathname.endsWith("/handoff/preview")) return previewResponse(url.searchParams.get("resolution_id"));
    });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Why is a new investigation needed?"), "Recheck after recovery");
    await user.click(screen.getByRole("button", { name: "Review request" }));
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await vi.waitFor(() => expect(readRoute()).toMatchObject({
      run: "00000000-0000-4000-8000-000000000021", resolution,
    }));
    expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
  });

  it("keeps a failed saved selection and retries it without falling back", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}?resolution=${resolution}`);
    let attempts = 0;
    const requested: (string | null)[] = [];
    mount([run()], "open", {}, true, (request) => {
      const url = new URL(request.url);
      if (!url.pathname.endsWith("/handoff/preview")) return;
      requested.push(url.searchParams.get("resolution_id"));
      if (++attempts === 1) return Response.json({ error: { code: "not_found", message: "Saved handoff unavailable" } }, { status: 404 });
      return previewResponse(url.searchParams.get("resolution_id"));
    });
    expect(await screen.findByText(/Saved handoff unavailable/)).toBeTruthy();
    expect(screen.queryByText("Current handoff")).toBeNull();
    expect(readRoute().resolution).toBe(resolution);
    await userEvent.setup().click(within(screen.getByLabelText("Handoff preview")).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
    expect(requested).toEqual([resolution, resolution]);
  });

  it("reaches a saved resolution beyond one hundred newer activities", async () => {
    const activity = (index: number) => ({
      id: index === 100 ? resolution : `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      kind: index === 100 ? "resolve" : "note", text: index === 100 ? human : `Note ${index}`,
      actor_id: "00000000-0000-4000-8000-000000000030", actor_name: "Pat", created_at: "2026-11-01T00:00:00Z",
      incident_id: incidentId,
    } satisfies components["schemas"]["ActivityView"]);
    window.history.replaceState(null, "", `/#/incidents/${incidentId}`);
    mount([run()], "open", {}, true, (request) => {
      const url = new URL(request.url);
      if (url.pathname.endsWith("/handoff/preview")) return previewResponse(url.searchParams.get("resolution_id"));
      if (url.pathname.endsWith("/activity")) {
        const start = Number(url.searchParams.get("cursor") || 0);
        return Response.json({ items: Array.from({ length: Math.min(25, 101 - start) }, (_, n) => activity(start + n)), total: 101, next_cursor: start < 100 ? String(start + 25) : null });
      }
    });
    const user = userEvent.setup();
    expect(await screen.findByText("101 activities recorded.")).toBeTruthy();
    for (let i = 0; i < 4; i++) {
      await user.click(await screen.findByRole("button", { name: "Load older activity" }));
      await screen.findByText(i === 3 ? human : `Note ${(i + 1) * 25}`);
    }
    const old = screen.getByText(human).closest("article")!;
    expect(within(old).getByText("Actor: Pat")).toBeTruthy();
    await user.click(within(old).getByRole("button", { name: "View saved resolution" }));
    expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Saved resolution handoff" }));
    expect(readRoute().resolution).toBe(resolution);
  });

  it("refreshes the current report after resolving", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}`);
    let resolved = false;
    const detail = () => ({
      id: incidentId, workload_id: workload.id, title: "Checkout restarts",
      description: "Original context", state: resolved ? "resolved" : "open",
      created_by: null, created_at: "2026-11-01T00:00:00Z",
      origin: { kind: "operator", source_id: null }, first_seen_at: null, last_seen_at: null,
      severity: { value: null, provenance: "not_provided" }, alert_signal: "none", workload,
    });
    mount([run()], "open", {}, true, (request) => {
      const url = new URL(request.url);
      if (request.method === "POST" && url.pathname.endsWith("/lifecycle")) {
        resolved = true;
        return Response.json(detail());
      }
      if (url.pathname === `/api/incidents/${incidentId}`) return Response.json(detail());
      if (url.pathname.endsWith("/handoff/preview")) return Response.json({
        incident_id: incidentId, resolution_id: null, schema_version: "HandoffSnapshot/v1",
        snapshot: {}, markdown: resolved ? "State: resolved; reason: service recovered" : "State: open",
      });
    });
    expect(await screen.findByText("State: open")).toBeTruthy();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Note or transition reason"), "service recovered");
    await user.click(screen.getByRole("button", { name: "Resolve with snapshot" }));
    expect(await screen.findByText("State: resolved; reason: service recovered")).toBeTruthy();
    expect(screen.queryByText("State: open")).toBeNull();
  });

  it("keeps history visible with retry when a post-mutation refetch fails", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}`);
    let posted = false;
    let unavailable = true;
    const old = {
      id: resolution, incident_id: incidentId, kind: "note", text: "Earlier operator context",
      actor_id: run().requested_by, actor_name: "Pat", created_at: "2026-11-01T00:00:00Z",
    };
    const added = { ...old, id: "00000000-0000-4000-8000-000000000098", text: "Later context" };
    mount([run()], "open", {}, true, (request) => {
      const path = new URL(request.url).pathname;
      if (request.method === "POST" && path.endsWith("/notes")) {
        posted = true;
        return Response.json(added, { status: 201 });
      }
      if (path.endsWith("/activity")) {
        if (posted && unavailable) return Response.json(
          { error: { code: "history_unavailable", message: "Temporary history outage" } }, { status: 503 },
        );
        return Response.json({ items: posted ? [added, old] : [old], total: posted ? 2 : 1, next_cursor: null });
      }
    });
    expect(await screen.findByText(old.text)).toBeTruthy();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Note or transition reason"), added.text);
    await user.click(screen.getByRole("button", { name: "Save note" }));
    const history = screen.getByLabelText("Incident activity");
    expect(await within(history).findByText(/Temporary history outage/)).toBeTruthy();
    expect(within(history).getByText(old.text)).toBeTruthy();
    unavailable = false;
    await user.click(within(history).getByRole("button", { name: "Retry" }));
    expect(await within(history).findByText(added.text)).toBeTruthy();
    expect(within(history).queryByText(/Temporary history outage/)).toBeNull();
  });

  it("does not announce an older copy as the newly selected handoff", async () => {
    window.history.replaceState(null, "", `/#/incidents/${incidentId}`);
    const savedActivity = {
      id: resolution, incident_id: incidentId, kind: "resolve", text: "Saved fixture",
      actor_id: "00000000-0000-4000-8000-000000000030", actor_name: "Pat",
      created_at: "2026-11-01T00:00:00Z",
    } satisfies components["schemas"]["ActivityView"];
    mount([run()], "open", {}, true, (request) => {
      const url = new URL(request.url);
      if (url.pathname.endsWith("/handoff/preview")) return previewResponse(url.searchParams.get("resolution_id"));
      if (url.pathname.endsWith("/activity")) return Response.json({ items: [savedActivity], total: 1, next_cursor: null });
    });
    const user = userEvent.setup();
    const { promise: pending, resolve: finishCopy } = Promise.withResolvers<void>();
    const copying = vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(pending);
    try {
      await user.click(await screen.findByRole("button", { name: "Copy handoff" }));
      await user.click(await screen.findByRole("button", { name: "View saved resolution" }));
      expect(await screen.findByText(`Saved: ${human}`)).toBeTruthy();
      await act(async () => { finishCopy(); });
      expect(screen.queryByText("Handoff copied.")).toBeNull();
    } finally {
      finishCopy();
      copying.mockRestore();
    }
  });

  it("does not replay a denied write or clipboard intent when permissions change", async () => {
    let posts = 0;
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === "POST") {
        posts++;
        return Response.json({ error: { code: "forbidden", message: "Permission denied" } }, { status: 403 });
      }
      if (new URL(request.url).pathname.endsWith("/activity")) return Response.json({ items: [], total: 0, next_cursor: null });
      return previewResponse(null);
    });
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    caches.push(cache);
    const renderActivity = (canMutate: boolean) => (
      <QueryClientProvider client={cache}>
        <IncidentActivity id={incidentId} state="open" canMutate={canMutate} selectedResolution="" onSelectResolution={() => {}} />
      </QueryClientProvider>
    );
    const view = render(renderActivity(true));
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Note or transition reason"), "Human note");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText(/Permission denied/)).toBeTruthy();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Clipboard denied"));
    await user.click(await screen.findByRole("button", { name: "Copy handoff" }));
    expect(await screen.findByText(/Clipboard denied/)).toBeTruthy();
    view.rerender(renderActivity(false));
    expect(screen.queryByRole("button", { name: "Save note" })).toBeNull();
    view.rerender(renderActivity(true));
    expect(posts).toBe(1);
    expect(copy).toHaveBeenCalledTimes(1);
    copy.mockRestore();
  });
});
