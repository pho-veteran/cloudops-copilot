// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import type * as ApiClient from "@/lib/api/client";
import { EvidenceCatalog } from "./EvidenceCatalog";
import { buildIncidentHash, safeRoute } from "@/app/routes";

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
const evidence = {
  id: evidenceId,
  run_id: runId,
  workload_uid: "uid",
  source_key: "development_fixture",
  source_kind: "fixture",
  catalog_version: "legacy_v1",
  schema_version: 1,
  payload_kind: "fixture",
  payload_version: 1,
  observed_at: null,
  collected_at: "2026-11-01T00:00:00Z",
  requested_window: null,
  time_relation: "not_captured",
  coverage: { status: "unavailable", reason_codes: ["not_captured"] },
  query_metadata: null,
  sha256: "0".repeat(64),
  redaction_policy_version: "legacy_v1",
  payload_ref: evidenceId,
  expires_at: "2026-12-01T00:00:00Z",
  pinned: true,
  content_status: "available",
} satisfies components["schemas"]["EvidenceView"];
const run = {
  id: runId,
  incident_id: incident,
  state: "insufficient_evidence",
  reason: "Evidence collection finished; analysis unavailable.",
  created_at: "2026-11-01T00:00:00Z",
  finished_at: "2026-11-01T00:00:01Z",
  requested_by: null,
  origin: { kind: "operator", source_id: null },
  question: null,
  question_source: "not_captured",
  window: null,
  queue_reason: null,
  outcome_code: null,
  evidence: [evidence],
  attempts: [],
  pack_coverage: null,
  progress: null,
  dispatch: null,
  cancel: { allowed: false, reason: "finished" },
  diagnosis: "unavailable",
  recovery: "unavailable",
} satisfies components["schemas"]["RunView"];
const clients: QueryClient[] = [];
function mount(
  selectedEvidence = "",
  value: components["schemas"]["RunView"] = run,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  clients.push(client);
  const rendered = render(
    <QueryClientProvider client={client}>
      <EvidenceCatalog
        incidentId={incident}
        run={value}
        selectedEvidence={selectedEvidence}
        selectedResolution=""
      />
    </QueryClientProvider>,
  );
  return { ...rendered, client };
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  http.fetch.mockReset();
  window.history.replaceState(null, "", "/");
});

it("links to exact historical evidence without fetching content until selected", () => {
  mount();
  expect(
    screen
      .getByRole("link", { name: "development_fixture" })
      .getAttribute("href"),
  ).toBe(`#/incidents/${incident}?run=${runId}&evidence=${evidenceId}`);
  expect(http.fetch).not.toHaveBeenCalled();
});

it("retains the comparison return context when selecting a different source", () => {
  window.location.hash = buildIncidentHash(incident, runId, "#/queue", "", evidenceId, {
    leftIncident: incident,
    leftRun: runId,
    rightIncident: incident,
    rightRun: "00000000-0000-4000-8000-000000000099",
    source: "kubernetes.state",
    catalog: "standard_v1",
    changedOnly: true,
    locatorKind: "field",
    locatorKey: `v1_${"a".repeat(64)}`,
  });
  mount();
  const route = safeRoute(screen.getByRole("link", { name: "development_fixture" }).getAttribute("href")!);
  expect(route.page).toBe("incidents");
  expect(route.evidence).toBe(evidenceId);
  expect(route.selection?.rightRun).toBe("00000000-0000-4000-8000-000000000099");
  expect(route.selection?.source).toBe("kubernetes.state");
  expect(route.selection?.changedOnly).toBe(true);
  expect(route.selection?.locatorKey).toBeUndefined();
});

it("identifies historical evidence with uncaptured source coverage as legacy", async () => {
  http.fetch.mockImplementation(async (input) => {
    const path = new URL((input as Request).url).pathname;
    if (path.endsWith("/content"))
      return Response.json({
        evidence_id: evidenceId,
        run_id: runId,
        schema_version: 1,
        payload_kind: "fixture",
        payload_version: 1,
        sha256: "abc",
        payload: { observation: "Synthetic fixture" },
      });
    return Response.json(evidence);
  });
  mount(evidenceId);
  await screen.findByText("Stored content");
  expect(screen.getByText("Coverage").nextElementSibling?.textContent).toBe(
    "Not captured (legacy)",
  );
  expect(
    screen.getByRole("link", { name: "development_fixture" }).parentElement
      ?.textContent,
  ).not.toContain("unavailable");
  expect(document.activeElement).toBe(
    screen.getByRole("heading", { name: "Selected evidence" }),
  );
});

it("shows an expiry race without claiming retained content", async () => {
  http.fetch.mockImplementation(async (input) =>
    new URL((input as Request).url).pathname.endsWith("/content")
      ? Response.json(
          { error: { code: "evidence_expired", message: "Expired" } },
          { status: 410 },
        )
      : Response.json(evidence),
  );
  mount(evidenceId);
  expect(await screen.findByText(/Evidence content has expired/)).toBeTruthy();
  expect(screen.queryByText("Stored content")).toBeNull();
});

it("does not switch an unknown evidence reference to the first source", async () => {
  const missing = "00000000-0000-4000-8000-000000000099";
  http.fetch.mockResolvedValue(
    Response.json(
      { error: { code: "not_found", message: "Evidence not found" } },
      { status: 404 },
    ),
  );
  mount(missing);
  expect(document.activeElement).toBe(
    screen.getByRole("heading", { name: "Selected evidence" }),
  );
  expect(await screen.findByText("Evidence not found")).toBeTruthy();
  await waitFor(() => expect(http.fetch).toHaveBeenCalledTimes(1));
  expect(
    new URL((http.fetch.mock.calls[0][0] as Request).url).pathname.endsWith(
      `/evidence/${missing}`,
    ),
  ).toBe(true);
});

it("does not replay an expired-session protected read", async () => {
  http.fetch.mockResolvedValue(
    Response.json(
      { error: { code: "unauthorized", message: "Session expired" } },
      { status: 401 },
    ),
  );
  mount(evidenceId);
  expect(await screen.findByText("Session expired")).toBeTruthy();
  expect(http.fetch).toHaveBeenCalledTimes(1);
});

it("updates pin and expiry from refreshed Run metadata instead of a stale detail copy", async () => {
  const initialRecord = { ...evidence, pinned: false };
  http.fetch.mockImplementation(async (input) =>
    new URL((input as Request).url).pathname.endsWith("/content")
      ? Response.json({
          evidence_id: evidenceId,
          run_id: runId,
          schema_version: 1,
          payload_kind: "fixture",
          payload_version: 1,
          sha256: evidence.sha256,
          payload: { observation: "Synthetic fixture" },
        })
      : Response.json(initialRecord),
  );
  const mounted = mount(evidenceId, { ...run, evidence: [initialRecord] });
  await screen.findByText("Stored content");
  expect(screen.getByText("Not pinned")).toBeTruthy();
  const sourceLink = screen.getByRole("link", { name: "development_fixture" });
  sourceLink.focus();
  mounted.rerender(
    <QueryClientProvider client={mounted.client}>
      <EvidenceCatalog
        incidentId={incident}
        run={{ ...run, evidence: [{ ...initialRecord, pinned: true }] }}
        selectedEvidence={evidenceId}
        selectedResolution=""
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText("Pinned")).toBeTruthy();
  expect(document.activeElement).toBe(sourceLink);
  mounted.rerender(
    <QueryClientProvider client={mounted.client}>
      <EvidenceCatalog
        incidentId={incident}
        run={{
          ...run,
          evidence: [
            {
              ...initialRecord,
              content_status: "expired",
              payload_ref: null,
            },
          ],
        }}
        selectedEvidence={evidenceId}
        selectedResolution=""
      />
    </QueryClientProvider>,
  );
  expect(
    screen.getByText(
      "Evidence content has expired; metadata remains available.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText("Stored content")).toBeNull();
});


it.each(["unavailable", "expired"] as const)("keeps metric timeline readable when content is %s", async (status) => {
  http.fetch.mockResolvedValue(Response.json({
    schema: "RunTimeline/v1", run_id: runId, window: null,
    markers: [{ kind: "window_start", at: evidence.collected_at, id: runId, label: "Captured window start" }],
    truncated: false, limitations: "Aligned by time; correlation only.",
  }));
  mount(evidenceId, { ...run, evidence: [{ ...evidence,
    schema_version: 2, source_key: "prometheus.request_rate", source_kind: "series", payload_kind: "series",
    content_status: status, payload_ref: null, coverage: { status: "unavailable", reason_codes: ["no_samples"] },
  }] });
  expect(await screen.findByText(/Captured window start/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByText(/Missing data is not zero/)).toBeTruthy();
  expect(http.fetch).toHaveBeenCalledTimes(1);
  expect(new URL((http.fetch.mock.calls[0][0] as Request).url).pathname.endsWith("/timeline")).toBe(true);
});
