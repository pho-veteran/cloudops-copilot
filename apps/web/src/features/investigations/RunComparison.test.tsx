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
import type * as ApiClient from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { buildIncidentHash, safeRoute } from "@/app/routes";
import { RunComparison } from "./RunComparison";
import { CitationTarget } from "./CitationTarget";

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
const left = "00000000-0000-4000-8000-000000000020";
const right = "00000000-0000-4000-8000-000000000030";
const evidence = "00000000-0000-4000-8000-000000000040";
const locator = { kind: "field" as const, key: `v1_${"a".repeat(64)}` };
const side = {
  incident_id: incident,
  workload_id: incident,
  workload_uid: "fixture-uid",
  cluster: "fixture",
  namespace: "demo",
  name: "checkout",
  state: "failed",
  window: null,
  question_source: "not_captured",
};
const result = {
  schema_version: "Compare/v1",
  algorithm: "semantic_v1",
  left: { ...side, run_id: left },
  right: { ...side, run_id: right },
  identity_relation: "same_identity",
  sources: [],
  items: [],
  next_cursor: null,
  reason: null,
  status: "comparable",
} satisfies components["schemas"]["RunComparison"];
const clients: QueryClient[] = [];
function mount(child: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>{child}</QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  http.fetch.mockReset();
  window.history.replaceState(null, "", "/");
});

it("requires explicit selection and performs no background comparison before it", () => {
  window.location.hash = buildIncidentHash(incident, left, "#/queue", "", "", { plan: evidence, execution: right });
  mount(<RunComparison incidentId={incident} runId={left} />);
  expect(http.fetch).not.toHaveBeenCalled();
  expect(
    (
      screen.getByRole("button", {
        name: "Compare selected runs",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("Right run ID"), {
    target: { value: right },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Compare selected runs" }),
  );
  expect(safeRoute(window.location.hash).selection?.rightRun).toBe(right);
  expect(safeRoute(window.location.hash).selection).toMatchObject({ plan: evidence, execution: right });
});

it("loads exact pair IDs outside history pagination and swaps them without creating work", async () => {
  window.location.hash = buildIncidentHash(incident, left, "#/queue", "", "", {
    rightIncident: incident,
    rightRun: right,
  });
  http.fetch.mockResolvedValue(
    new Response(JSON.stringify(result), { status: 200 }),
  );
  mount(<RunComparison incidentId={incident} runId={left} />);
  await screen.findByRole("heading", { name: "Left Run" });
  expect(
    new URL((http.fetch.mock.calls[0][0] as Request).url).searchParams.get(
      "right_run_id",
    ),
  ).toBe(right);
  fireEvent.click(screen.getByRole("button", { name: "Swap left and right" }));
  expect(safeRoute(window.location.hash).selection).toMatchObject({
    leftRun: right,
    rightRun: left,
  });
  expect(
    http.fetch.mock.calls.every(
      ([request]) => (request as Request).method === "GET",
    ),
  ).toBe(true);
});

it("does not carry a saved resolution into the other incident when swapping", async () => {
  const otherIncident = "00000000-0000-4000-8000-000000000050";
  const resolution = "00000000-0000-4000-8000-000000000060";
  window.location.hash = buildIncidentHash(incident, left, "#/queue", resolution, "", {
    leftIncident: incident,
    leftRun: left,
    rightIncident: otherIncident,
    rightRun: right,
    source: "kubernetes.state",
    catalog: "standard_v1",
    changedOnly: true,
  });
  http.fetch.mockResolvedValue(Response.json({
    ...result,
    right: { ...result.right, incident_id: otherIncident },
  }));
  mount(<RunComparison incidentId={incident} runId={left} />);
  await screen.findByRole("heading", { name: "Left Run" });
  fireEvent.click(screen.getByRole("button", { name: "Swap left and right" }));
  const route = safeRoute(window.location.hash);
  expect(route.page).toBe("incidents");
  expect(route.id).toBe(otherIncident);
  expect(route.resolution).toBe("");
  expect(route.selection).toMatchObject({
    leftIncident: otherIncident,
    leftRun: right,
    rightIncident: incident,
    rightRun: left,
    source: "kubernetes.state",
    changedOnly: true,
  });
});

it.each(["available", "expired"])("labels legacy coverage separately from %s content", async (contentStatus) => {
  window.location.hash = buildIncidentHash(incident, left, "#/queue", "", "", {
    rightIncident: incident,
    rightRun: right,
  });
  const legacy = {
    id: evidence,
    run_id: left,
    schema_version: 1,
    coverage: { status: "unavailable", reason_codes: ["not_captured"] },
    content_status: contentStatus,
  };
  http.fetch.mockResolvedValue(Response.json({
    ...result,
    sources: [{
      source_key: "development_fixture",
      catalog_version: "legacy_v1",
      status: "unavailable",
      left: legacy,
      right: { ...legacy, run_id: right },
    }],
  }));
  mount(<RunComparison incidentId={incident} runId={left} />);
  expect(await screen.findByText(
    `Left: Not captured (legacy) · ${contentStatus} · Right: Not captured (legacy) · ${contentStatus}`,
  )).toBeTruthy();
});

it("shows a replacement identity without inventing aligned observations", async () => {
  window.location.hash = buildIncidentHash(incident, left, "#/queue", "", "", {
    rightIncident: incident,
    rightRun: right,
  });
  http.fetch.mockResolvedValue(
    new Response(
      JSON.stringify({
        ...result,
        identity_relation: "replacement",
        status: "not_comparable",
        reason: "workload_identity_replaced",
      }),
      { status: 200 },
    ),
  );
  mount(<RunComparison incidentId={incident} runId={left} />);
  expect(
    await screen.findByText(/Values are not aligned across UIDs/),
  ).toBeTruthy();
  expect(screen.queryByRole("link", { name: /Open exact/ })).toBeNull();
});

it("does not replace a missing pair with the latest Run or retry an expired session", async () => {
  window.location.hash = buildIncidentHash(incident, left, "#/queue", "", "", {
    rightIncident: incident,
    rightRun: right,
  });
  http.fetch.mockResolvedValue(
    new Response(
      JSON.stringify({
        error: { code: "session_expired", message: "Session expired." },
      }),
      { status: 401 },
    ),
  );
  mount(<RunComparison incidentId={incident} runId={left} />);
  expect(await screen.findByText("Session expired.")).toBeTruthy();
  expect(http.fetch).toHaveBeenCalledTimes(1);
  expect(safeRoute(window.location.hash).selection?.rightRun).toBe(right);
});

it("focuses the exact resolved citation region and leaves target-not-found explicit", async () => {
  window.location.hash = buildIncidentHash(
    incident,
    left,
    "#/queue",
    "",
    evidence,
    { locatorKind: locator.kind, locatorKey: locator.key },
  );
  http.fetch.mockImplementation(
    async (request) =>
      new Response(
        JSON.stringify(
          (request as Request).url.includes("citation-targets")
            ? { items: [], next_cursor: null }
            : {
                citation: { run_id: left, evidence_id: evidence, locator },
                target: { label: "deployment/replicas", value: 2 },
                evidence: { id: evidence, sha256: "fixture-hash" },
              },
        ),
        { status: 200 },
      ),
  );
  mount(
    <CitationTarget incidentId={incident} runId={left} evidenceId={evidence} />,
  );
  await screen.findByText("deployment/replicas");
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { name: "Exact citation target" }),
    ),
  );
  expect(screen.getByText("2")).toBeTruthy();
});

it("does not steal citation focus when the comparison response arrives later", async () => {
  window.location.hash = buildIncidentHash(
    incident,
    left,
    "#/queue",
    "",
    evidence,
    {
      rightIncident: incident,
      rightRun: right,
      locatorKind: "field",
      locatorKey: locator.key,
    },
  );
  http.fetch.mockResolvedValue(
    new Response(JSON.stringify(result), { status: 200 }),
  );
  mount(<RunComparison incidentId={incident} runId={left} />);
  await screen.findByRole("heading", { name: "Left Run" });
  expect(document.activeElement).not.toBe(
    screen.getByRole("heading", { name: "Compare runs" }),
  );
});
