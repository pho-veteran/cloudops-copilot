// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { Reports } from "./Reports";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>(), download: vi.fn() }));
vi.mock("@/lib/download", () => ({ download: http.download }));
vi.mock("@/lib/api/client", async (originalImport) => {
  const original = await originalImport<typeof import("@/lib/api/client")>();
  return { ...original, api: original.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client };
});
let cache: QueryClient;
const identifier = "00000000-0000-4000-8000-000000000099";
function report(final: boolean): components["schemas"]["ReportView"] {
  return {
    id: identifier, workload_id: "00000000-0000-4000-8000-000000000001",
    state: final ? "unavailable" : "queued", requested_hours: 6, tool_version: "krr-v1.31.1",
    result: { recommendations: null }, reason: "Representative history is unqualified.",
    created_by: "00000000-0000-4000-8000-000000000002", created_at: "2026-11-17T09:00:00Z",
    finished_at: final ? "2026-11-17T09:01:00Z" : null, schema_version: 2,
    input_identity: null, profile: null, result_v2: null, migration_disposition: null,
    job_phase: final ? "finished" : "queued", result_state: final ? "unavailable" : null,
    attempts: [], dispatch_paused: false, dispatch_reason: null,
    applicability: { state: "unknown", assessed_at: "2026-11-17T09:01:00Z", reasons: ["source_window_unavailable"] },
    export_ready: final, export_sha256: null,
  };
}
function mount(final: boolean, failExport = false) {
  http.fetch.mockImplementation(async (input) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.includes("/export")) return failExport
      ? Response.json({ error: { code: "report_not_final", message: "This report has no finalized export yet." } }, { status: 409 })
      : new Response('{"stored":"exact selected terminal bytes"}');
    if (url.includes("recommendation-reports")) {
      const page: components["schemas"]["ReportPage"] = {
        items: [report(final)], next_cursor: null,
        observation: { component: "recommendations", status: "unavailable", observed_at: "2026-11-17T09:01:00Z", last_success_at: null, reason: "runner_unconfigured", source_kind: "unconfigured" },
      };
      return Response.json(page);
    }
    return Response.json({ items: [], next_cursor: null });
  });
  render(<QueryClientProvider client={cache}><Reports canMutate={false} /></QueryClientProvider>);
}
beforeEach(() => {
  http.fetch.mockReset(); http.download.mockReset();
  cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(() => { cleanup(); cache.clear(); });
it("reads the paged transport and disables a pending export with an explanation", async () => {
  mount(false);
  const button = await screen.findByRole("button", { name: /Export report/ });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/export becomes available when this report finishes/i)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Generate/ })).toBeNull();
  expect(http.download).not.toHaveBeenCalled();
});
it("downloads the exact selected stored text and exposes a failed export without success", async () => {
  mount(true);
  await userEvent.click(await screen.findByRole("button", { name: /Export report/ }));
  await vi.waitFor(() => expect(http.download).toHaveBeenCalledWith('{"stored":"exact selected terminal bytes"}', `resource-report-${identifier}.json`, "application/json"));
  cleanup(); cache.clear(); http.download.mockClear();
  mount(true, true);
  await userEvent.click(await screen.findByRole("button", { name: /Export report/ }));
  expect(await screen.findByText(/no finalized export yet/)).toBeTruthy();
  expect(http.download).not.toHaveBeenCalled();
});
