// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type * as ApiClient from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { MetricEvidence } from "./MetricEvidence";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof ApiClient>();
  return {
    ...module,
    api: module.createTransport({
      baseUrl: "http://localhost",
      fetch: http.fetch,
    }).client,
  };
});

const incident = "00000000-0000-4000-8000-000000000010";
const runId = "00000000-0000-4000-8000-000000000020";
const evidenceId = "00000000-0000-4000-8000-000000000030";
const start = "2026-01-01T00:00:00Z";
const end = "2026-01-01T00:30:00Z";
const startSeconds = Date.parse(start) / 1000;
const endSeconds = Date.parse(end) / 1000;

const window = { start, end, source: "preset_30" as const };
const evidence = {
  id: evidenceId,
  run_id: runId,
  workload_uid: "uid-1",
  source_key: "prometheus.request_rate",
  source_kind: "series",
  catalog_version: "standard_v1",
  schema_version: 2,
  payload_kind: "series",
  payload_version: 1,
  observed_at: end,
  collected_at: end,
  requested_window: window,
  time_relation: "within_requested_window",
  coverage: { status: "available", reason_codes: [] },
  query_metadata: {
    query_id: "request_rate",
    step: 30,
    unit: "requests/s",
    warnings: [],
    window,
  },
  sha256: "0".repeat(64),
  redaction_policy_version: "v1",
  payload_ref: evidenceId,
  expires_at: null,
  pinned: false,
  content_status: "available",
} satisfies components["schemas"]["EvidenceView"];

function content(series: unknown[]) {
  return {
    evidence_id: evidenceId,
    run_id: runId,
    schema_version: 2,
    payload_kind: "series",
    payload_version: 1,
    sha256: "0".repeat(64),
    payload: { series },
  } satisfies components["schemas"]["EvidenceContent"];
}

const oneSeries = [
  {
    uid: "series-1",
    name: "requests",
    pod: "checkout-1",
    unit: "requests/s",
    samples: [
      [startSeconds, 1],
      [startSeconds + 900, 4],
      [endSeconds, 2],
    ],
  },
];

function timeline(markers: unknown[] = []) {
  return {
    schema: "RunTimeline/v1",
    run_id: runId,
    window,
    markers: [
      { kind: "window_start", at: start, id: evidenceId, label: "Captured window start" },
      { kind: "window_end", at: end, id: evidenceId, label: "Captured window end" },
      ...markers,
    ],
    truncated: false,
    limitations: "Aligned by time; correlation only.",
  };
}

const clients: QueryClient[] = [];
function mount(
  value = content(oneSeries),
  evidenceValue: components["schemas"]["EvidenceView"] = evidence,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MetricEvidence
        incidentId={incident}
        runId={runId}
        evidence={evidenceValue}
        content={value}
        window={window}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  http.fetch.mockReset();
});

it("renders a bounded chart, accessible table and persisted timeline markers", async () => {
  http.fetch.mockResolvedValue(
    Response.json(
      timeline([
        {
          kind: "rollout_observation",
          at: start,
          id: runId,
          label: "Rollout observation",
          detail: { health: "healthy", coverage: "complete", generation: 3 },
        },
      ]),
    ),
  );
  mount();
  expect(screen.getByRole("img", { name: /requests · checkout-1 over/ })).toBeTruthy();
  expect(screen.getByRole("table")).toBeTruthy();
  expect(screen.getByText(/Timeline markers/)).toBeTruthy();
  expect((await screen.findAllByText(/Rollout observation/)).length).toBeGreaterThan(0);
  expect(screen.getByText(/correlation, not causality/)).toBeTruthy();
  expect(
    new URL((http.fetch.mock.calls[0][0] as Request).url).pathname.endsWith(
      "/timeline",
    ),
  ).toBe(true);
});

it("does not render missing samples as a zero value", () => {
  http.fetch.mockResolvedValue(Response.json(timeline()));
  mount(content([{ ...oneSeries[0], samples: [] }]));
  expect(screen.getByText(/No finite samples were returned/)).toBeTruthy();
  expect(screen.getByText(/Missing data is not zero/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.queryByRole("table")).toBeNull();
});

it("surfaces unproven ownership and warnings without drawing foreign values", () => {
  http.fetch.mockResolvedValue(Response.json(timeline()));
  mount(
    content(oneSeries),
    {
      ...evidence,
      coverage: { status: "partial", reason_codes: ["ownership_unproven"] },
      query_metadata: {
        ...evidence.query_metadata,
        warnings: ["2 malformed sample entries were omitted."],
      },
    },
  );
  expect(
    screen.getByText(/Returned series did not match the configured workload UID/),
  ).toBeTruthy();
  expect(screen.getAllByText(/malformed sample entries/).length).toBeGreaterThan(0);
});

it("selects a series explicitly and keeps the table on that series", () => {
  http.fetch.mockResolvedValue(Response.json(timeline()));
  const second = {
    uid: "series-2",
    name: "errors",
    pod: "checkout-2",
    unit: "errors/s",
    samples: [
      [startSeconds, 0],
      [endSeconds, 1],
    ],
  };
  mount(content([...oneSeries, second]));
  const buttons = screen.getAllByRole("button");
  expect(buttons.map((button) => button.textContent)).toEqual([
    "requests · checkout-1",
    "errors · checkout-2",
  ]);
  fireEvent.click(screen.getByRole("button", { name: "errors · checkout-2" }));
  expect(
    screen.getByRole("button", { name: "errors · checkout-2" }).getAttribute("aria-pressed"),
  ).toBe("true");
  expect(screen.getByRole("img", { name: /errors · checkout-2 over/ })).toBeTruthy();
  expect(screen.getByRole("table").textContent).toContain("errors");
});

it("keeps the chart visible when the timeline read fails", async () => {
  http.fetch.mockResolvedValue(
    Response.json(
      { error: { code: "session_expired", message: "Session expired." } },
      { status: 401 },
    ),
  );
  mount();
  expect(await screen.findByText("Session expired.")).toBeTruthy();
  expect(screen.getByRole("img")).toBeTruthy();
  expect(http.fetch).toHaveBeenCalledTimes(1);
});
