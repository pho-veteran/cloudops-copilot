// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { RecoveryPlans } from "./RecoveryPlans";
import { recoveryAvailabilityFixture, reviewClockFixture } from "./fixtures";

const network = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  const transport = module.createTransport({
    fetch: (request, init) => network.fetch(request, init),
  });
  return { ...module, api: transport.client };
});
let cache: QueryClient;
afterEach(() => {
  cleanup();
  cache?.clear();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "#/queue");
});

function mount(overrides: { template?: string; origin?: string | null; valid?: boolean; viewer?: boolean; malformed?: boolean; foreign?: boolean; reviewMs?: number; candidate?: "scale_deployment" | "set_memory_profile" | "restore_deployment_template" } = {}) {
  const state = {
    planId: "00000000-0000-4000-8000-000000000001",
    failed: false,
    approved: false,
    template: "scale_deployment",
    origin: "operator_manual",
    valid: true,
    viewer: false, malformed: false, foreign: false, reviewMs: 60000,
    ...overrides,
  };
  network.fetch.mockImplementation(async (input) => {
    const path = new URL((input as Request).url).pathname;
    if ((input as Request).method === "POST" && path.endsWith("/decision")) {
      state.approved = true;
      return Response.json({ id: "00000000-0000-4000-8000-000000000008", plan_id: state.planId,
        workload_id: "00000000-0000-4000-8000-000000000004", state: "queued", holds_lock: true,
        result: {}, created_at: new Date().toISOString(), finished_at: null,
        outcome: { schema_version: "RecoveryOutcome/v1", operation_id: "00000000-0000-4000-8000-000000000008", attribution: "not_sent", verification: "not_performed", future_authority: "target_locked", reason: "Controlled queued decision receipt." } });
    }
    if ((input as Request).method === "POST") return Response.json({ state: "manual_only", reason: "Controlled applicability result.", next_step: "Inspect the original scope." }, { status: 201 });
    const readId = path.startsWith("/api/plans/") ? path.split("/").at(-1)! : state.planId;
    const plan = {
      id: readId,
      incident_id: state.foreign ? "00000000-0000-4000-8000-000000000099" : "00000000-0000-4000-8000-000000000003",
      workload_id: "00000000-0000-4000-8000-000000000004",
      rationale: "Controlled proposal",
      version: 1,
      template: state.template,
      origin: state.origin,
      source_analysis_id: null,
      forward_execution_id: null,
      digest: readId.repeat(2).replaceAll("-", ""),
      state: readId === state.planId && !state.approved ? "proposed" : "approved",
      expires_at: new Date(Date.now() + 300_000).toISOString(),
      content: { source: "controlled_ui_fixture", replicas: 2 },
    };
    if (path.endsWith("/plans")) return Response.json([plan]);
    if (path.endsWith("/executions")) {
      if (state.approved) await new Promise(resolve => setTimeout(resolve, 80));
      return Response.json([]);
    }
    if (path.endsWith("/recovery-availability")) return Response.json(state.malformed ? {} : recoveryAvailabilityFixture(plan.workload_id));
    if (path === "/api/recovery-policy") return Response.json({ policy: { memory_profiles: [{ id: "controlled-memory", version: "1", workload_uid: "controlled-deployment-uid", request: "64Mi", limit: "128Mi" }] }, digest: null, verification_profiles: [] });
    if (path.endsWith("/baselines")) return Response.json({ items: [{ id: "00000000-0000-4000-8000-000000000007", profile: "deployment-images-resources/v1", confirmed_at: "2026-11-16T12:00:00Z" }], next_cursor: null });
    if (path.endsWith("/analysis")) return Response.json({ status: "completed", analysis_id: "00000000-0000-4000-8000-000000000006", result: { status: "supported", candidate_template_id: state.candidate } });
    if (state.failed) throw new TypeError("Controlled network interruption");
    return Response.json({
      plan,
      valid: state.valid && readId === state.planId,
      reason: "Controlled provider observation",
      review_token: "fixture-receipt",
      ...reviewClockFixture(),
      review_expires_at: new Date(Date.now() + state.reviewMs).toISOString(),
    });
  });
  cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <RecoveryPlans id="00000000-0000-4000-8000-000000000003" workloadId="00000000-0000-4000-8000-000000000004" canMutate={!state.viewer} live runId={state.candidate ? "00000000-0000-4000-8000-000000000005" : undefined} latestRunId="00000000-0000-4000-8000-000000000005" />
    </QueryClientProvider>,
  );
  return state;
}

it("shows advice preview provenance without enabling execution or hiding manual scale", async () => {
  mount({ template: "restore_deployment_template", origin: "analysis_advice", valid: false });
  expect(await screen.findByText("restore deployment template · analysis advice")).toBeDefined();
  expect((screen.getByRole("button", { name: "Review approval" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("button", { name: "Prepare live scale plan" })).toBeDefined();
});

it("closes old approval intent when a different immutable plan becomes current", async () => {
  const state = mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "Review approval" }),
  );
  expect(screen.getByRole("dialog")).toBeDefined();
  state.planId = "00000000-0000-4000-8000-000000000002";
  await act(async () => {
    await cache.invalidateQueries({ queryKey: ["plans"] });
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    network.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});

it("discards confirmation and disables stale review data when a fresh read fails", async () => {
  const state = mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "Review approval" }),
  );
  state.failed = true;
  await act(async () => {
    await cache.invalidateQueries({ queryKey: ["plan-review"] });
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    (
      screen.getByRole("button", {
        name: "Review approval",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    network.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});

it("keeps Viewer history read-only without proposal, approval or inverse controls", async () => {
  mount({ viewer: true });
  await screen.findByText(/Plan version 1/);
  expect(screen.queryByRole("button", { name: "Review approval" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Prepare live scale plan" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Read exact plan again" }));
  expect(network.fetch.mock.calls.every(([request]) => (request as Request).method === "GET")).toBe(true);
});

it("does not report successful approval as an expired review during refresh", async () => {
  mount();
  await userEvent.click(await screen.findByRole("button", { name: "Review approval" }));
  await userEvent.click(screen.getByRole("button", { name: "Approve this version and dispatch" }));
  await screen.findByText("Plan version 1 · approved");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(cache.isMutating()).toBe(0));
  expect(screen.queryByText(/The exact review expired or changed/)).toBeNull();
  expect(network.fetch.mock.calls.filter(([request]) => (request as Request).method === "POST")).toHaveLength(1);
});

it("fails closed with useful guidance for an incomplete authority response", async () => {
  mount({ malformed: true });
  await screen.findByText(/Current target authority is incomplete/);
  expect((screen.getByRole("button", { name: "Review approval" }) as HTMLButtonElement).disabled).toBe(true);
  expect(network.fetch.mock.calls.every(([request]) => (request as Request).method === "GET")).toBe(true);
});

it("does not offer a decision for a foreign incident's selected record", async () => {
  mount({ foreign: true });
  await screen.findByText(/This record belongs to another incident/);
  expect(screen.queryByRole("button", { name: "Review approval" })).toBeNull();
});

it("uses the server receipt clock despite a device clock far in the future", async () => {
  mount();
  const button = await screen.findByRole("button", { name: "Review approval" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2040-01-01T00:00:00Z"));
  await userEvent.type(screen.getByLabelText("Reason for the proposed change"), "Fresh reason");
  expect((button as HTMLButtonElement).disabled).toBe(false);
});

it("discards an expired receipt without replay and focuses recovery feedback", async () => {
  mount({ reviewMs: 1500 });
  await userEvent.click(await screen.findByRole("button", { name: "Review approval" }));
  expect(screen.getByRole("dialog")).toBeDefined();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull(), { timeout: 3500 });
  await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("Recovery action feedback")));
  expect(network.fetch.mock.calls.every(([request]) => (request as Request).method === "GET")).toBe(true);
});

it("retains an explicit historical plan when newer plans refresh", async () => {
  const old = "00000000-0000-4000-8000-000000000008";
  window.history.replaceState(null, "", `#/incidents/00000000-0000-4000-8000-000000000003?plan=${old}`);
  const state = mount();
  await screen.findByText(new RegExp(old));
  state.planId = "00000000-0000-4000-8000-000000000009";
  await act(async () => { await cache.invalidateQueries({ queryKey: ["plans"] }); });
  expect(screen.getByText(new RegExp(old))).toBeDefined();
  const reads = network.fetch.mock.calls.map(([request]) => new URL((request as Request).url).pathname).filter(path => path.startsWith("/api/plans/"));
  expect(reads.every(path => path === `/api/plans/${old}`)).toBe(true);
});

it("does not replace a historical selection after an older proposal response arrives", async () => {
  const state = mount();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Reason for the proposed change"), "Prepare a new exact plan");
  const button = screen.getByRole("button", { name: "Prepare live scale plan" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  const original = network.fetch.getMockImplementation()!;
  let release: ((response: Response) => void) | undefined;
  network.fetch.mockImplementation((request, init) => (request as Request).method === "POST" ? new Promise<Response>(resolve => { release = resolve; }) : original(request, init));
  const captured = cache.getQueryData<{ plan: Record<string, unknown> }>(["plan-review", state.planId])!.plan;
  await user.click(button);
  await waitFor(() => expect(release).toBeDefined());
  const old = "00000000-0000-4000-8000-000000000008";
  window.history.replaceState(null, "", `#/incidents/00000000-0000-4000-8000-000000000003?plan=${old}`);
  state.planId = "00000000-0000-4000-8000-000000000009";
  await act(async () => { release!(Response.json({ ...captured, id: state.planId }, { status: 201 })); });
  await screen.findByText(new RegExp(old));
  expect(window.location.hash).toContain(`plan=${old}`);
});

it.each(["scale_deployment", "set_memory_profile", "restore_deployment_template"] as const)("submits only the exact %s advice selector without calling the model", async candidate => {
  mount({ candidate });
  await userEvent.type(screen.getByLabelText("Reason for the proposed change"), "Inspect exact retained advice");
  if (candidate === "set_memory_profile") await userEvent.selectOptions(await screen.findByLabelText("Qualified memory profile"), "controlled-memory");
  if (candidate === "restore_deployment_template") await userEvent.selectOptions(await screen.findByLabelText("Exact confirmed baseline"), "00000000-0000-4000-8000-000000000007");
  const button = await screen.findByRole("button", { name: "Prepare exact advice plan" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  await userEvent.click(button);
  await screen.findByText(/Controlled applicability result/);
  const writes = network.fetch.mock.calls.map(([request]) => request as Request).filter(request => request.method === "POST");
  expect(writes).toHaveLength(1);
  expect(new URL(writes[0].url).pathname).toBe("/api/incidents/00000000-0000-4000-8000-000000000003/plans");
  expect(await writes[0].clone().json()).toEqual({ origin: "analysis_advice", run_id: "00000000-0000-4000-8000-000000000005", analysis_id: "00000000-0000-4000-8000-000000000006", template: candidate, rationale: "Inspect exact retained advice", ...(candidate === "scale_deployment" ? { replicas: 2 } : candidate === "set_memory_profile" ? { profile_id: "controlled-memory" } : { baseline_id: "00000000-0000-4000-8000-000000000007" }) });
});
