// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { RecoveryPlans } from "./RecoveryPlans";

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
});

function mount() {
  const state = {
    planId: "00000000-0000-4000-8000-000000000001",
    failed: false,
  };
  network.fetch.mockImplementation(async (input) => {
    const path = new URL((input as Request).url).pathname;
    const plan = {
      id: state.planId,
      version: 1,
      digest: state.planId.repeat(2).replaceAll("-", ""),
      state: "proposed",
      expires_at: new Date(Date.now() + 300_000).toISOString(),
      content: { source: "controlled_ui_fixture", replicas: 2 },
    };
    if (path.endsWith("/plans")) return Response.json([plan]);
    if (path.endsWith("/executions")) return Response.json([]);
    if (state.failed) throw new TypeError("Controlled network interruption");
    return Response.json({
      plan,
      valid: true,
      reason: "Controlled provider observation",
      review_token: "fixture-receipt",
    });
  });
  cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <RecoveryPlans id="00000000-0000-4000-8000-000000000003" canMutate live />
    </QueryClientProvider>,
  );
  return state;
}

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
