// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import supported from "../../../../../contracts/fixtures/diagnosis/supported.json";
import eligible from "../../../../../contracts/fixtures/diagnosis/quota-eligible.json";
import early from "../../../../../contracts/fixtures/diagnosis/quota-early.json";
import { AnalysisPanel, AnalysisSummary, type Analysis } from "./Analysis";
import { ModelConnection } from "./ModelConnection";
import { safeRoute } from "@/app/routes";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  return {
    ...module,
    api: module.createTransport({
      baseUrl: "http://localhost",
      fetch: http.fetch,
    }).client,
  };
});
const caches: QueryClient[] = [];
function mount(node: React.ReactNode) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 3 } },
  });
  caches.push(cache);
  return render(
    <QueryClientProvider client={cache}>{node}</QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((c) => c.clear());
  http.fetch.mockReset();
  window.history.replaceState(null, "", "/");
});
const analysis = supported.analysis as Analysis;
const incidentId = analysis.result!.incident_id;
const runId = analysis.result!.run_id;
const posts = () =>
  http.fetch.mock.calls.filter(([r]) => (r as Request).method === "POST");

it("renders stored advice as inert text and links the exact telemetry target", () => {
  const hostile = structuredClone(analysis);
  hostile.result!.summary =
    "<script>alert(1)</script> [unsafe](javascript:alert(1))";
  mount(<AnalysisSummary analysis={hostile} />);
  expect(screen.getByText(hostile.result!.summary)).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
  const link = screen.getAllByRole("link", { name: /exact evidence/ })[0];
  const ref = analysis.result!.supporting[0].citation;
  expect(safeRoute(link.getAttribute("href")!)).toMatchObject({
    id: incidentId,
    run: ref.run_id,
    evidence: ref.evidence_id,
    selection: { locatorKind: ref.locator!.kind, locatorKey: ref.locator!.key },
  });
  expect(screen.getByText(/This is not an executable plan/)).toBeTruthy();
});

it.each([
  [false, eligible],
  [true, early],
])(
  "honors role and server retry denial without sending a resume (%s)",
  async (canMutate, fixture) => {
    http.fetch.mockImplementation(async () => Response.json(fixture.analysis));
    mount(
      <AnalysisPanel
        incidentId={incidentId}
        runId={runId}
        canMutate={canMutate}
      />,
    );
    await screen.findByRole("heading", { name: "Waiting for provider quota" });
    const button = screen.queryByRole("button", { name: "Resume analysis" });
    expect(button === null || (button as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(posts()).toHaveLength(0);
  },
);

it("uses an empty idempotent explicit resume and never replays it after 401", async () => {
  http.fetch.mockImplementation(async (r) =>
    (r as Request).method === "POST"
      ? Response.json(
          { error: { code: "session_expired", message: "Sign in again." } },
          { status: 401 },
        )
      : Response.json(eligible.analysis),
  );
  const view = mount(
    <AnalysisPanel incidentId={incidentId} runId={runId} canMutate />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Resume analysis" }),
  );
  await screen.findByText("Sign in again.");
  expect(posts()).toHaveLength(1);
  const request = posts()[0][0] as Request;
  expect(await request.clone().json()).toEqual({});
  expect(request.headers.get("idempotency-key")).toMatch(/^[a-f0-9-]{36}$/);
  view.unmount();
  mount(<AnalysisPanel incidentId={incidentId} runId={runId} canMutate />);
  await screen.findByRole("heading", { name: "Waiting for provider quota" });
  await waitFor(() => expect(posts()).toHaveLength(1));
});

it("separates transport, schema and quality and preserves missing usage for Viewers", async () => {
  http.fetch.mockImplementation(async (r) =>
    Response.json(
      (r as Request).url.endsWith("/status")
        ? supported.model_status
        : {
            items: [
              {
                id: "probe",
                state: "completed",
                created_at: "2026-11-15",
                receipt: {
                  transport_success: true,
                  schema_success: false,
                  usage: { total_tokens: null },
                },
              },
            ],
          },
    ),
  );
  mount(<ModelConnection canMutate={false} />);
  expect(
    await screen.findByText(
      /Transport: Succeeded · Schema: Not validated · Observed total tokens: Unavailable/,
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(/probe does not establish diagnostic quality/),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Check configured model" }),
  ).toBeNull();
  expect(posts()).toHaveLength(0);
});
