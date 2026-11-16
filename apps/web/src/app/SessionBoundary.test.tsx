// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Workspace } from "./FoundationApp";
import { transport } from "@/lib/api/client";
import { buildKnowledgeHash } from "./routes";
import {
  documentFixture,
  ids,
  pageFixture,
  statusFixture,
} from "@/features/knowledge/fixtures";

const network = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  const transport = original.createTransport({
    fetch: (request, init) => network.fetch(request, init),
    onExpired: () =>
      window.dispatchEvent(new Event("cloudops:session-expired")),
  });
  return { ...original, transport, api: transport.client };
});

const a = {
  principal: { id: "a", username: "operator", role: "operator" },
  csrf_token: "a-csrf",
  expires_at: "2026-10-26T23:00:00Z",
};
const b = {
  ...a,
  principal: { id: "b", username: "viewer", role: "viewer" },
  csrf_token: "b-csrf",
};
const expired = () =>
  Response.json(
    { error: { code: "session_expired", message: "Sign in again." } },
    { status: 401 },
  );
let cache: QueryClient;
function mount() {
  cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <Workspace />
    </QueryClientProvider>,
  );
}
function path(request: RequestInfo | URL) {
  return new URL((request as Request).url).pathname;
}
beforeEach(() => {
  vi.clearAllMocks();
  transport.clearSession();
  window.history.replaceState(null, "", "/");
  window.scrollTo = vi.fn();
  network.fetch.mockImplementation(async (request) => {
    return Response.json(
      path(request) === "/api/session" ? a : { items: [], next_offset: null },
    );
  });
});
afterEach(() => {
  cleanup();
  cache?.clear();
  transport.clearSession();
});

it("clears exact Knowledge content on expiry and rereads the safe version as Viewer", async () => {
  const hash = buildKnowledgeHash({ document: ids.document, search: "memory" });
  window.location.hash = hash;
  let viewer = false;
  network.fetch.mockImplementation(async (r) => {
    const url = path(r);
    if (url === "/api/login") viewer = true;
    if (url === "/api/session" || url === "/api/login")
      return Response.json(viewer ? b : a);
    if (url.endsWith("index-status")) return Response.json(statusFixture);
    if (url.endsWith("/documents")) return Response.json(pageFixture);
    return Response.json(documentFixture);
  });
  mount();
  await screen.findByRole("heading", { name: documentFixture.title });
  expect(
    cache.getQueryCache().findAll({ queryKey: ["knowledge"] }).length,
  ).toBeGreaterThan(0);
  act(() => window.dispatchEvent(new Event("cloudops:session-expired")));
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  expect(
    screen.queryByRole("heading", { name: documentFixture.title }),
  ).toBeNull();
  expect(
    cache.getQueryCache().findAll({ queryKey: ["knowledge"] }),
  ).toHaveLength(0);
  const reads = network.fetch.mock.calls.filter(([r]) =>
    path(r).endsWith(ids.document),
  ).length;
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Username"), "viewer");
  await user.type(screen.getByLabelText("Password"), "local-test-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("heading", { name: documentFixture.title });
  expect(window.location.hash).toBe(hash);
  expect(
    network.fetch.mock.calls.filter(([r]) => path(r).endsWith(ids.document))
      .length,
  ).toBeGreaterThan(reads);
  expect(
    network.fetch.mock.calls
      .map(([r]) => r as Request)
      .filter((r) => r.method === "POST")
      .map((r) => path(r)),
  ).toEqual(["/api/login"]);
});

it("rechecks authority after returning to a tab and hides stale mutations until 401 is known", async () => {
  mount();
  await screen.findByRole("button", { name: "New incident" });
  let release!: (value: Response) => void;
  network.fetch.mockImplementation(
    async () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      }),
  );
  act(() => {
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("focus"));
  });
  expect(screen.queryByRole("button", { name: "New incident" })).toBeNull();
  await waitFor(() => expect(release).toBeDefined());
  await act(async () => {
    release(expired());
  });
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  expect(screen.getByRole("status").textContent).toContain(
    "Your session ended",
  );
  expect(
    network.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});

it("keeps a failed authority recheck in an explicit retry state", async () => {
  mount();
  await screen.findByRole("button", { name: "New incident" });
  network.fetch.mockRejectedValueOnce(new TypeError("offline"));
  act(() => {
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("focus"));
  });
  expect((await screen.findByRole("alert")).textContent).toContain(
    "Cannot reach the local API",
  );
  expect(screen.queryByRole("button", { name: "New incident" })).toBeNull();
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByRole("button", { name: "New incident" });
});

it("changes to another tab's current account using fresh reads", async () => {
  mount();
  await screen.findByRole("button", { name: "New incident" });
  cache.setQueryData(["old-protected-record"], { secret: "a" });
  network.fetch.mockImplementation(async (r) =>
    Response.json(
      path(r) === "/api/session" ? b : { items: [], next_offset: null },
    ),
  );
  act(() => {
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("focus"));
  });
  await screen.findByText(/Viewer access:/);
  expect(screen.queryByRole("button", { name: "New incident" })).toBeNull();
  expect(cache.getQueryData(["old-protected-record"])).toBeUndefined();
  expect(cache.getQueryData(["session"])).toEqual(b);
});

it("signs out A, signs in B and excludes a deliberately late A query", async () => {
  let release!: (value: Response) => void;
  let account = a;
  network.fetch.mockImplementation(async (r) => {
    const request = r as Request;
    if (path(r) === "/api/incidents")
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    if (path(r) === "/api/logout") {
      account = b;
      return new Response(null, { status: 204 });
    }
    if (path(r) === "/api/login") return Response.json(b);
    return Response.json(account);
  });
  mount();
  const user = userEvent.setup();
  await screen.findByRole("button", { name: "Sign out" });
  await waitFor(() => expect(release).toBeDefined());
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  await user.type(screen.getByLabelText("Username"), "viewer");
  await user.type(screen.getByLabelText("Password"), "local-test-password");
  // Let B's queue read complete normally; A's outstanding fetch still ignores cancellation.
  network.fetch.mockImplementation(async (r) =>
    Response.json(
      path(r) === "/api/incidents" ? { items: [], next_offset: null } : b,
    ),
  );
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByText(/Viewer access:/);
  await act(async () => {
    release(
      Response.json({
        items: [
          {
            id: "private-a",
            title: "A private record",
            state: "open",
            created_at: "2026-10-26T00:00:00Z",
          },
        ],
        next_offset: null,
      }),
    );
  });
  expect(screen.queryByText("A private record")).toBeNull();
  expect(cache.getQueryData(["session"])).toEqual(b);
  expect(
    JSON.stringify(
      cache
        .getQueryCache()
        .getAll()
        .map((q) => q.state.data),
    ),
  ).not.toContain("private-a");
});

it("keeps navigation usable without discarding authority on a route change", async () => {
  mount();
  await screen.findByRole("button", { name: "New incident" });
  act(() => {
    window.location.hash = "/workloads";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await screen.findByRole("heading", { name: "Workloads" });
  expect(screen.getByRole("button", { name: "New incident" })).toBeDefined();
});

it("returns to the selected Run after Find guidance expires without replaying the action", async () => {
  const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";
  const run = "00000000-0000-4000-8000-000000000002";
  window.history.replaceState(null, "", `/#/incidents/${id}?run=${run}`);
  const fixture = (r: RequestInfo | URL) => {
    const url = path(r);
    if (url.endsWith("/knowledge")) return { state: "not_selected", selection: null, reason: null };
    if (url.endsWith("/activity")) return { items: [], total: 0, next_cursor: null };
    if (url.endsWith("/handoff/preview")) return { incident_id: id, resolution_id: null, schema_version: "HandoffSnapshot/v2", snapshot: {}, markdown: "Current handoff" };
    if (url.endsWith("/plans") || url.endsWith("/executions")) return [];
    if (url === "/api/session" || url === "/api/login") return a;
    if (url.endsWith(`/runs/${run}`))
      return {
        id: run,
        state: "insufficient_evidence",
        evidence: [],
        pack_coverage: null,
        attempts: [],
        origin: { kind: "operator", source_id: null },
        created_at: "2026-10-26T00:00:00Z",
      };
    if (url.endsWith("/runs"))
      return {
        items: [{ id: run, state: "insufficient_evidence" }],
        next_offset: null,
      };
    return {
      id,
      title: "Saved incident",
      state: "open",
      origin: { kind: "operator", source_id: null },
      workload: { active: true, namespace: "demo", name: "checkout" },
    };
  };
  network.fetch.mockImplementation(async (r) =>
    (r as Request).method === "POST" && path(r).endsWith("/knowledge")
      ? expired()
      : Response.json(fixture(r)),
  );
  mount();
  await screen.findByRole("heading", { name: "Saved incident" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Find guidance" }));
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  await user.type(screen.getByLabelText("Username"), "operator");
  await user.type(screen.getByLabelText("Password"), "local-test-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("heading", { name: "Saved incident" });
  expect(window.location.hash).toBe(`#/incidents/${id}?run=${run}`);
  const requests = network.fetch.mock.calls.map(([r]) => r as Request);
  expect(
    requests.filter((r) => r.method === "POST").map((r) => path(r)),
  ).toEqual([`/api/incidents/${id}/runs/${run}/knowledge`, "/api/login"]);
  expect(
    requests.filter((r) => path(r) === `/api/incidents/${id}`),
  ).toHaveLength(2);
});

it("discards an expired approval intent and requires a fresh plan read and explicit approval after sign-in", async () => {
  const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";
  const planId = "00000000-0000-4000-8000-000000000004";
  window.history.replaceState(null, "", `/#/incidents/${id}`);
  let signedInAgain = false;
  let decisions = 0;
  const plan = {
    id: planId,
    template: "scale_deployment",
    origin: "operator_manual",
    source_analysis_id: null,
    version: 1,
    digest: "a".repeat(64),
    state: "proposed",
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    content: { replicas: 2, source: "controlled_ui_fixture" },
  };
  network.fetch.mockImplementation(async (r) => {
    const request = r as Request;
    const url = path(r);
    if (url === "/api/login") signedInAgain = true;
    if (url === "/api/session" || url === "/api/login")
      return Response.json(
        signedInAgain ? { ...a, csrf_token: "new-session-csrf" } : a,
      );
    if (url.endsWith("/decision")) {
      decisions++;
      return decisions === 1
        ? expired()
        : Response.json({ ...plan, state: "approved" });
    }
    if (url === `/api/plans/${planId}`)
      return Response.json({
        plan,
        valid: true,
        reason: "Controlled live-provider fixture",
        review_token: signedInAgain ? "fresh-plan-receipt" : "old-plan-receipt",
      });
    if (url.endsWith("/plans")) return Response.json([plan]);
    if (url.endsWith("/activity")) return Response.json({ items: [], total: 0, next_cursor: null });
    if (url.endsWith("/handoff/preview")) return Response.json({ incident_id: id, resolution_id: null, schema_version: "HandoffSnapshot/v2", snapshot: {}, markdown: "Current handoff" });
    if (url.endsWith("/executions")) return Response.json([]);
    if (url.endsWith("/runs"))
      return Response.json({ items: [], next_offset: null });
    return Response.json({
      id,
      title: "Approval session fixture",
      state: "open",
      origin: { kind: "operator", source_id: null },
      workload: {
        active: true,
        namespace: "demo",
        name: "checkout",
        source: "live_kubernetes",
      },
    });
  });
  mount();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: "Review approval" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Approve this version and dispatch" }),
  );
  await screen.findByRole("heading", { name: "Sign in to your workspace" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(decisions).toBe(1);
  await user.type(screen.getByLabelText("Username"), "operator");
  await user.type(screen.getByLabelText("Password"), "local-test-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("heading", { name: "Approval session fixture" });
  expect(decisions).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(window.location.hash).toBe(`#/incidents/${id}`);
  await user.click(
    await screen.findByRole("button", { name: "Review approval" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Approve this version and dispatch" }),
  );
  await waitFor(() => expect(decisions).toBe(2));
  const requests = network.fetch.mock.calls
    .map(([r]) => r as Request)
    .filter((r) => path(r).endsWith("/decision"));
  expect((await requests[0].clone().json()).review_token).toBe(
    "old-plan-receipt",
  );
  expect((await requests[1].clone().json()).review_token).toBe(
    "fresh-plan-receipt",
  );
  expect(requests[1].headers.get("X-CSRF-Token")).toBe("new-session-csrf");
});
