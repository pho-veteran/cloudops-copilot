// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { workloadFixture } from "@/features/workloads/fixtures";
import { clearRequestKeys } from "./request";
import { InvestigationWorkspace } from "./InvestigationWorkspace";

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

function mount(runs: ReturnType<typeof run>[], state = "open") {
  const posts: { path: string; key: string | null; body: unknown }[] = [];
  http.fetch.mockImplementation(async (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
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
    if (path.endsWith("/runs")) return Response.json({ items: runs, next_offset: null });
    if (path.includes("/runs/")) return Response.json(runs.find((r) => path.endsWith(r.id)));
    if (path.endsWith("/plans") || path.endsWith("/executions") || path.endsWith("/activity"))
      return Response.json([]);
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
    });
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  render(
    <QueryClientProvider client={cache}>
      <InvestigationWorkspace id={incidentId} selectedRun="" canMutate />
    </QueryClientProvider>,
  );
  return posts;
}

describe("investigation requests", () => {
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
});
