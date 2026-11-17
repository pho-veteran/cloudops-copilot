// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { buildReportHash, safeRoute, type ReportRoute } from "@/app/routes";
import { Reports } from "./Reports";
import { reportFixture, reportId, reportWorkload } from "./fixtures";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>(), download: vi.fn() }));
vi.mock("@/lib/download", () => ({ download: http.download }));
vi.mock("@/lib/api/client", async originalImport => {
  const original = await originalImport<typeof import("@/lib/api/client")>();
  return { ...original, api: original.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client };
});
let cache: QueryClient;
let records: components["schemas"]["ReportView"][];
let exportResponse: () => Promise<Response>;
let createResponse: () => Promise<Response>;
let detailFailure = false;
let workloadRecords: components["schemas"]["WorkloadView"][];
const pageCursor = (offset: number) => btoa(JSON.stringify({ offset }));
const exactExport = '{"stored":"exact selected terminal bytes"}';

function Harness({ canMutate }: { canMutate: boolean }) {
  const [route, setRoute] = useState<ReportRoute>(() => safeRoute(window.location.hash).reports ?? {});
  useEffect(() => {
    const change = () => setRoute(safeRoute(window.location.hash).reports ?? {});
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  return <Reports canMutate={canMutate} route={route} />;
}
function mount(route: ReportRoute = {}, canMutate = false) {
  window.history.replaceState(null, "", "/" + buildReportHash(route));
  return render(<QueryClientProvider client={cache}><Harness canMutate={canMutate} /></QueryClientProvider>);
}
function posts() { return http.fetch.mock.calls.filter(([request]) => request instanceof Request && request.method === "POST"); }
function deferred() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  http.fetch.mockReset(); http.download.mockReset(); records = [reportFixture()]; detailFailure = false; workloadRecords = [reportWorkload];
  cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  exportResponse = async () => new Response(exactExport);
  createResponse = async () => {
    const row: components["schemas"]["ReportView"] = { ...reportFixture(101), state: "queued", job_phase: "queued", result_state: null, result_v2: null, export_ready: false, export_sha256: null, finished_at: null };
    records.unshift(row);
    return Response.json(row, { status: 201 });
  };
  http.fetch.mockImplementation(async input => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname.endsWith("/export")) return exportResponse();
    if (url.pathname === "/api/recommendation-reports") {
      if (request.method === "POST") return createResponse();
      const cursor = url.searchParams.get("cursor");
      let offset = 0;
      try { offset = cursor ? JSON.parse(atob(cursor)).offset as number : 0; }
      catch { return Response.json({ error: { code: "cursor_invalid", message: "Use the cursor with its original workload filter." } }, { status: 422 }); }
      const filtered = records.filter(r => !url.searchParams.get("workload_id") || r.workload_id === url.searchParams.get("workload_id"));
      return Response.json({ items: filtered.slice(offset, offset + 25), next_cursor: filtered.length > offset + 25 ? pageCursor(offset + 25) : null, observation: { component: "recommendations", status: "unavailable", observed_at: "2026-11-17T07:00:00Z", last_success_at: null, reason: "runner_unavailable", source_kind: "live_krr" } });
    }
    if (url.pathname.startsWith("/api/recommendation-reports/")) {
      if (detailFailure) return Response.json({ error: { code: "source_unavailable", message: "Controlled refresh failure" } }, { status: 503 });
      const row = records.find(r => r.id === url.pathname.split("/").pop());
      return row ? Response.json(row) : Response.json({ error: { code: "report_not_found", message: "Selected report not found" } }, { status: 404 });
    }
    if (url.pathname === "/api/workloads") return Response.json({ items: workloadRecords.filter(w => w.active), next_offset: null });
    return Response.json(workloadRecords.find(w => w.id === url.pathname.split("/").pop()) ?? reportWorkload);
  });
});
afterEach(() => { cleanup(); cache.clear(); });

it("renders mixed advice, zero, unset, excluded memory, envelope and stale applicability independently", async () => {
  mount({ report: reportId(99) });
  const cpu = await screen.findByRole("region", { name: "app cpu advice" });
  expect(within(cpu).getByText("0 cores")).toBeTruthy();
  expect(within(cpu).getAllByText("Unset")).toHaveLength(2);
  expect(within(cpu).getByText(/360\/360 distinct/)).toBeTruthy();
  const memory = screen.getByRole("region", { name: "app memory advice" });
  expect(within(memory).getAllByText(/Unavailable · recent oom/)).toHaveLength(2);
  expect(within(memory).getByText(/24 MiB/)).toBeTruthy();
  expect(screen.getByText("Stored outcome: available")).toBeTruthy();
  expect(screen.getByText("Applicability: stale")).toBeTruthy();
  expect(screen.getByText("Actual query envelope")).toBeTruthy();
  expect(screen.getByText(/23:58:45 UTC/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Generate/ })).toBeNull();
});

it("denies pending export with an explanation and preserves selection after raced409", async () => {
  records = [{ ...reportFixture(), state: "queued", job_phase: "queued", result_state: null, result_v2: null, export_ready: false, export_sha256: null, finished_at: null }];
  mount({ report: reportId(99) });
  const button = await screen.findByRole("button", { name: /Export selected report/ });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/Queued and running reports cannot be exported/)).toBeTruthy();
  expect(http.download).not.toHaveBeenCalled();
  records[0] = reportFixture();
  exportResponse = async () => Response.json({ error: { code: "report_not_final", message: "This report has no finalized export yet." } }, { status: 409 });
  await act(async () => { await cache.invalidateQueries({ queryKey: ["recommendation-report"] }); });
  await userEvent.click(button);
  expect(await screen.findByText(/no finalized export yet/)).toBeTruthy();
  expect(window.location.hash).toBe(buildReportHash({ report: reportId(99) }));
  expect(http.download).not.toHaveBeenCalled();
});

it("exports exact stored text and suppresses a delayed export after another selection", async () => {
  records.push(reportFixture(100));
  mount({ report: reportId(99) });
  await userEvent.click(await screen.findByRole("button", { name: /Export selected report/ }));
  await vi.waitFor(() => expect(http.download).toHaveBeenCalledWith(exactExport, `resource-report-${reportId(99)}.json`, "application/json"));
  http.download.mockClear();
  const waiting = deferred(); exportResponse = () => waiting.promise;
  await userEvent.click(screen.getByRole("button", { name: /Export selected report/ }));
  await userEvent.click(screen.getByRole("link", { name: new RegExp(reportId(100)) }));
  window.location.hash = buildReportHash({ report: reportId(100) });
  await act(async () => { window.dispatchEvent(new Event("hashchange")); });
  await vi.waitFor(() => expect(screen.getByRole("region", { name: "Selected resource report" }).textContent).toContain(reportId(100)));
  await act(async () => { waiting.resolve(new Response(exactExport)); });
  expect(http.download).not.toHaveBeenCalled();
});

it("creates only on explicit Operator action and ignores a late response after navigation", async () => {
  const waiting = deferred(); createResponse = () => waiting.promise;
  mount({ workload: reportWorkload.id }, true);
  const button = await screen.findByRole("button", { name: "Generate resource report" });
  await vi.waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  expect(posts()).toHaveLength(0);
  await userEvent.click(button);
  expect(posts()).toHaveLength(1);
  expect(await (posts()[0][0] as Request).clone().json()).toEqual({ workload_id: reportWorkload.id, requested_hours: 6 });
  window.location.hash = buildReportHash({ workload: reportWorkload.id, report: reportId(99) });
  await act(async () => { window.dispatchEvent(new Event("hashchange")); });
  await act(async () => { waiting.resolve(Response.json({ ...reportFixture(101), state: "queued", job_phase: "queued", result_state: null, result_v2: null, export_ready: false, export_sha256: null, finished_at: null }, { status: 201 })); });
  expect(window.location.hash).toBe(buildReportHash({ workload: reportWorkload.id, report: reportId(99) }));
  expect(posts()).toHaveLength(1);
});

it("traverses more than50 reports and restores an exact older selection from the URL", async () => {
  records = Array.from({ length: 60 }, (_, index) => reportFixture(index + 200));
  mount();
  await userEvent.click(await screen.findByRole("button", { name: "Next report page" }));
  await vi.waitFor(() => expect(window.location.hash).toContain("cursor=" + encodeURIComponent(pageCursor(25))));
  await vi.waitFor(() => expect(http.fetch.mock.calls.some(([r]) => (r as Request).url.includes("cursor=" + encodeURIComponent(pageCursor(25))))).toBe(true));
  await userEvent.click(screen.getByRole("button", { name: "Next report page" }));
  await vi.waitFor(() => expect(window.location.hash).toContain("cursor=" + encodeURIComponent(pageCursor(50))));
  window.location.hash = buildReportHash({ cursor: pageCursor(50), report: reportId(259) });
  await act(async () => { window.dispatchEvent(new Event("hashchange")); });
  await vi.waitFor(() => expect(screen.getByRole("region", { name: "Selected resource report" }).textContent).toContain(reportId(259)));
  cleanup(); cache.clear();
  mount({ cursor: pageCursor(50), report: reportId(259) });
  await vi.waitFor(() => expect(screen.getByRole("region", { name: "Selected resource report" }).textContent).toContain(reportId(259)));
  expect(window.location.hash).toContain("cursor=" + encodeURIComponent(pageCursor(50)));
  expect(posts()).toHaveLength(0);
});

it("retains known report data after refresh failure and never substitutes latest for a missing ID", async () => {
  mount({ report: reportId(99) });
  await screen.findByRole("region", { name: "app cpu advice" });
  detailFailure = true;
  await act(async () => { await cache.invalidateQueries({ queryKey: ["recommendation-report"] }); });
  expect(await screen.findByText("Controlled refresh failure")).toBeTruthy();
  expect(screen.getByRole("region", { name: "app cpu advice" })).toBeTruthy();
  detailFailure = false;
  await userEvent.click(within(screen.getByRole("region", { name: "Selected resource report" })).getByRole("button", { name: "Retry" }));
  await vi.waitFor(() => expect(screen.queryByText("Controlled refresh failure")).toBeNull());
  window.location.hash = buildReportHash({ report: reportId(404) });
  await act(async () => { window.dispatchEvent(new Event("hashchange")); });
  expect(await screen.findByText("Selected report not found")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "app cpu advice" })).toBeNull();
  expect(window.location.hash).toContain(reportId(404));
});

it("keeps empty and legacy histories readable without inventing versioned advice", async () => {
  records = [];
  mount();
  expect(await screen.findByText(/No saved reports yet/)).toBeTruthy();
  cleanup(); cache.clear();
  records = [{ ...reportFixture(), schema_version: 1, input_identity: null, profile: null, result_v2: null, migration_disposition: "legacy_terminal", result_state: null, state: "finished", job_phase: "finished" }];
  mount({ report: reportId(99) });
  expect(await screen.findByText("Legacy report")).toBeTruthy();
  expect(screen.getByText(/not qualified versioned resource advice/)).toBeTruthy();
  expect(screen.getByText("Stored outcome: Legacy")).toBeTruthy();
  expect((screen.getByRole("button", { name: /Export selected report/ }) as HTMLButtonElement).disabled).toBe(false);
});


it("selects a newly admitted report after explicit generation and keeps it on refresh", async () => {
  mount({ workload: reportWorkload.id }, true);
  const button = await screen.findByRole("button", { name: "Generate resource report" });
  await vi.waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  await userEvent.click(button);
  await vi.waitFor(() => expect(window.location.hash).toContain(reportId(101)));
  expect((await screen.findByRole("button", { name: /Export selected report/ }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Generate resource report" }) as HTMLButtonElement).disabled).toBe(true);
  cleanup(); cache.clear();
  mount({ workload: reportWorkload.id, report: reportId(101) }, true);
  expect(await screen.findByText(reportId(101), { exact: true })).toBeTruthy();
  expect(posts()).toHaveLength(1);
});

it("retains an inactive same-name UID history and never generates for its active replacement implicitly", async () => {
  const old = { ...reportWorkload, id: reportId(3), uid: "controlled-retired-uid", active: false, retirement_reason: "replaced" as const };
  workloadRecords.push(old);
  const row = reportFixture(100);
  row.workload_id = old.id;
  row.input_identity = { ...row.input_identity!, workload_id: old.id, uid: old.uid };
  row.result_v2 = { ...row.result_v2!, identity: row.input_identity };
  records.push(row);
  mount({ report: row.id }, true);
  await userEvent.click(await screen.findByRole("link", { name: "Workload report history" }));
  expect(await screen.findByText(/This saved identity is inactive/)).toBeTruthy();
  expect((screen.getByLabelText("Workload history") as HTMLSelectElement).value).toBe(old.id);
  expect((screen.getByRole("button", { name: "Generate resource report" }) as HTMLButtonElement).disabled).toBe(true);
  expect(within(screen.getByRole("region", { name: "Saved workload identity" })).getByText(/controlled-retired-uid/)).toBeTruthy();
  await userEvent.selectOptions(screen.getByLabelText("Workload history"), reportWorkload.id);
  await vi.waitFor(() => expect(window.location.hash).toBe(buildReportHash({ workload: reportWorkload.id })));
  expect(screen.queryByRole("region", { name: "Selected resource report" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("does not replay a rejected generation when mutation controls become available again", async () => {
  createResponse = async () => Response.json({ error: { code: "session_expired", message: "Sign in again before generating a report." } }, { status: 401 });
  const view = mount({ workload: reportWorkload.id }, true);
  const button = await screen.findByRole("button", { name: "Generate resource report" });
  await vi.waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  await userEvent.click(button);
  expect(await screen.findByText(/Sign in again before generating/)).toBeTruthy();
  view.rerender(<QueryClientProvider client={cache}><Harness canMutate={false} /></QueryClientProvider>);
  view.rerender(<QueryClientProvider client={cache}><Harness canMutate={true} /></QueryClientProvider>);
  expect(posts()).toHaveLength(1);
  expect(window.location.hash).toBe(buildReportHash({ workload: reportWorkload.id }));
});


it("recovers an invalid history cursor without dropping the selected report", async () => {
  mount({ report: reportId(99), cursor: "invalid-but-bounded" });
  expect(await screen.findByText(/original workload filter/)).toBeTruthy();
  await screen.findByRole("region", { name: "app cpu advice" });
  await userEvent.click(screen.getByRole("button", { name: "First report page" }));
  await vi.waitFor(() => expect(window.location.hash).toBe(buildReportHash({ report: reportId(99) })));
  expect(screen.getByRole("region", { name: "app cpu advice" })).toBeTruthy();
  await vi.waitFor(() => expect(screen.queryByText(/original workload filter/)).toBeNull());
  expect(posts()).toHaveLength(0);
});
