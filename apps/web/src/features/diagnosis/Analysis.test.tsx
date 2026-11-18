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
import { buildIncidentHash, safeRoute } from "@/app/routes";

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
        ? {
            ...supported.model_status,
            configuration: {
              mode: "operational",
              api_root: "https://gateway.example/v1",
              credential_reference: "MODEL_TOKEN",
              timeout_seconds: 30,
              max_input_bytes: 65536,
              max_response_bytes: 262144,
              output_limit_parameter: "max_tokens",
              output_limit: 512,
              policy_id: "fixture-policy",
              policy_version: "2",
              policy_digest: "safe-policy-digest",
              max_request_bytes: 64000,
              max_target_bytes: 32000,
            },
            latest_usage: null,
            validated_rca: null,
            last_check: {
              id: "probe",
              state: "completed",
              created_at: "2026-11-15T00:00:00Z",
              deadline: "2026-11-15T00:15:00Z",
              identity: supported.model_status.identity,
              receipt: {
                transport_success: true,
                schema_success: false,
                observed_at: "2026-11-15T00:00:00Z",
              },
            },
          }
        : {
            items: [
              {
                id: "probe",
                state: "completed",
                created_at: "2026-11-15",
                receipt: {
                  transport_success: true,
                  schema_success: false,
                  observed_at: "2026-11-15T00:00:00Z",
                  usage_status: "observed",
                  usage: { total_tokens: null },
                },
              },
            ],
          },
    ),
  );
  mount(<ModelConnection canMutate={false} />);
  expect(
    await screen.findByText(/Transport: Succeeded · Probe marker: Invalid · Usage: Unavailable/),
  ).toBeTruthy();
  expect(screen.getByText(/No matching validated operational RCA/)).toBeTruthy();
  expect(screen.getByText(/Probe marker validation failed/)).toBeTruthy();
  expect(screen.getByText(/Diagnosis quality: qualified/)).toBeTruthy();
  expect(screen.getByText(/API root: https:\/\/gateway.example\/v1/)).toBeTruthy();
  expect(screen.getByText(/remaining provider quota is inferred/)).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Check configured model" }),
  ).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("keeps valid probe, unqualified RCA, paused dispatch, zero usage and provider guidance distinct", async () => {
  http.fetch.mockImplementation(async (r) =>
    Response.json(
      (r as Request).url.endsWith("/status")
        ? {
            ...supported.model_status,
            availability: "available",
            quality: "not_established",
            qualification_cohort: null,
            dispatch_allowed: false,
            dispatch_reason: "dispatch_paused",
            latest_usage: {
              source: "analysis",
              observed_at: "2026-11-16T12:00:00Z",
              status: "observed",
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
            latest_call: {
              source: "analysis",
              observed_at: "2026-11-16T12:00:00Z",
              kind: "text",
              http_status: 200,
            },
            validated_rca: null,
            last_check: {
              id: "probe",
              state: "completed",
              created_at: "2026-11-16T12:00:00Z",
              deadline: "2026-11-16T12:15:00Z",
              identity: supported.model_status.identity,
              receipt: {
                transport_success: true,
                schema_success: true,
                observed_at: "2026-11-16T12:00:00Z",
              },
            },
            configuration: {
              mode: "operational",
              api_root: "https://gateway.example/v1",
              credential_reference: "MODEL_TOKEN",
              timeout_seconds: 12,
              max_input_bytes: 40000,
              max_response_bytes: 8000,
              output_limit_parameter: "max_completion_tokens",
              output_limit: 600,
              policy_id: "egress-policy",
              policy_version: "3",
              policy_digest: "digest",
              max_request_bytes: 38000,
              max_target_bytes: 30000,
            },
          }
        : {
            items: [
              {
                id: "probe",
                state: "completed",
                created_at: "2026-11-16T12:00:00Z",
                receipt: {
                  transport_success: true,
                  schema_success: true,
                  observed_at: "2026-11-16T12:00:00Z",
                  usage_status: "observed",
                  usage: { total_tokens: 0 },
                },
              },
            ],
          },
    ),
  );
  mount(<ModelConnection canMutate={false} />);
  expect(await screen.findByText(/Transport availability: available/)).toBeTruthy();
  expect(screen.getByText(/Diagnosis quality: not established/)).toBeTruthy();
  expect(screen.getByText(/Valid harmless probe marker/)).toBeTruthy();
  expect(screen.getByText(/No matching validated operational RCA/)).toBeTruthy();
  expect(screen.getByText(/Paused · dispatch paused/)).toBeTruthy();
  expect(screen.getByText(/0 total tokens · analysis/)).toBeTruthy();
  expect(screen.getByText(/Latest provider call: text · analysis/)).toBeTruthy();
  expect(screen.getByText(/Usage: 0 total tokens/)).toBeTruthy();
  expect(screen.getByText(/Credential reference: MODEL_TOKEN/)).toBeTruthy();
  expect(screen.getByText(/Provider quota and remaining balance are unavailable/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it.each([false, true])(
  "scopes saved resolution in comparison findings links (same incident: %s)",
  (sameIncident) => {
    const other = "00000000-0000-4000-8000-000000000001";
    const resolution = "00000000-0000-4000-8000-000000000002";
    window.location.hash = buildIncidentHash(
      sameIncident ? incidentId : other,
      runId,
      "#/queue",
      resolution,
    );
    mount(<AnalysisSummary analysis={analysis} />);
    const link = screen.getAllByRole("link", { name: /exact evidence/ })[0];
    expect(safeRoute(link.getAttribute("href")!)).toMatchObject({
      id: incidentId,
      run: runId,
      resolution: sameIncident ? resolution : "",
    });
  },
);
