// @vitest-environment jsdom
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { Observations } from "./Observations";

type ObservationView = components["schemas"]["WorkloadObservationView"];
type ObservationPayload = components["schemas"]["WorkloadObservationPayload"];
type ObservationPage = components["schemas"]["WorkloadObservationPage"];
type IncidentPage = components["schemas"]["IncidentPage"];

const fixtureHttp = vi.hoisted(() => ({ fetch: vi.fn<typeof globalThis.fetch>() }));
vi.mock("@/lib/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...original,
    api: original.createTransport({
      baseUrl: "http://localhost",
      fetch: fixtureHttp.fetch,
    }).client,
  };
});

const WORKLOAD_ID = "00000000-0000-4000-8000-0000000000a1";
const PARTIAL_ID = "00000000-0000-4000-8000-0000000000a2";
const STALE_ID = "00000000-0000-4000-8000-0000000000a3";
const UNAVAILABLE_ID = "00000000-0000-4000-8000-0000000000a4";
const INCIDENT_ID = "11111111-1111-4111-8111-111111111111";

function payload(
  overrides: Partial<ObservationPayload> = {},
): ObservationPayload {
  return {
    application_telemetry: "unverified",
    available_replicas: 2,
    config_references: [
      { kind: "config_map", name: "checkout-config", source: "deployment" },
    ],
    coverage: { complete: true, missing_sources: [] },
    deployment_uid: "deployment-checkout-v1",
    desired_replicas: 2,
    generation: 4,
    observed_generation: 4,
    pods: [
      {
        uid: "pod-checkout-1",
        name: "checkout-6d9f-abc",
        phase: "Running",
        ready: true,
        containers: [
          {
            name: "app",
            init: false,
            ready: true,
            restart_count: 1,
            waiting_reason: null,
          },
        ],
      },
    ],
    ready_containers: 1,
    ready_replicas: 2,
    reasons: [],
    resource_version: "42",
    restart_delta: 0,
    restart_total: 1,
    status: "healthy",
    template_fields: {
      service_account_name: "checkout",
      containers: [
        {
          name: "app",
          image: "checkout:1.2.3",
          image_pull_policy: "IfNotPresent",
          env: ["DATABASE_URL"],
          resources: {
            requests: { cpu: "100m", memory: "128Mi" },
            limits: { cpu: "500m", memory: "256Mi" },
          },
          ports: [{ name: "http", container_port: 8080 }],
        },
      ],
      init_containers: [],
    },
    total_containers: 1,
    total_replicas: 2,
    updated_replicas: 2,
    ...overrides,
  };
}

function view(overrides: Partial<ObservationView> = {}): ObservationView {
  return {
    availability: "available",
    freshness: "fresh",
    last_attempt_at: "2026-10-26T10:00:30Z",
    last_attempt_reason: null,
    last_attempt_state: "succeeded",
    observation: payload(),
    observed_at: "2026-10-26T10:00:30Z",
    pending_refresh: false,
    source: "live_kubernetes",
    workload_active: true,
    workload_id: WORKLOAD_ID,
    ...overrides,
  };
}

function incidentPage(id = INCIDENT_ID): IncidentPage {
  return {
    items: [
      {
        id,
        workload_id: WORKLOAD_ID,
        title: "Checkout latency",
        description: "Checkout requests are slow.",
        state: "open",
        created_at: "2026-10-26T09:00:00Z",
        created_by: "22222222-2222-4222-8222-222222222222",
      },
    ],
    next_offset: null,
  };
}

type Fixture = {
  views?: Record<string, ObservationView>;
  histories?: Record<string, ObservationPage>;
  incidents?: Record<string, IncidentPage>;
  onRefresh?: (id: string) => void;
};

function route(fixture: Fixture) {
  fixtureHttp.fetch.mockImplementation(async request => {
    const req = request as Request;
    const url = new URL(req.url);
    const observation = /^\/api\/workloads\/([^/]+)\/observation$/.exec(
      url.pathname,
    );
    if (observation && req.method === "GET") {
      const value = fixture.views?.[observation[1]];
      if (!value)
        return Response.json(
          {
            error: {
              code: "workload_not_found",
              message: "Workload not found.",
            },
          },
          { status: 404 },
        );
      return Response.json(value);
    }
    const history = /^\/api\/workloads\/([^/]+)\/observations$/.exec(
      url.pathname,
    );
    if (history && req.method === "GET")
      return Response.json(
        fixture.histories?.[history[1]] ?? { items: [], next_offset: null },
      );
    const refresh = /^\/api\/workloads\/([^/]+)\/observation-refresh$/.exec(
      url.pathname,
    );
    if (refresh && req.method === "POST") {
      fixture.onRefresh?.(refresh[1]);
      return Response.json(
        {
          workload_id: refresh[1],
          status: "pending",
          requested_at: "2026-10-26T10:01:00Z",
        },
        { status: 202 },
      );
    }
    if (url.pathname === "/api/incidents") {
      const id = url.searchParams.get("workload_uuid") ?? "";
      return Response.json(
        fixture.incidents?.[id] ?? { items: [], next_offset: null },
      );
    }
    return Response.json(
      { error: { code: "not_found", message: "Not found." } },
      { status: 404 },
    );
  });
}

const caches: QueryClient[] = [];
function mount(ui: ReactNode) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  caches.push(cache);
  return render(
    <QueryClientProvider client={cache}>{ui}</QueryClientProvider>,
  );
}

beforeEach(() => {
  fixtureHttp.fetch.mockReset();
});
afterEach(() => {
  cleanup();
  caches.splice(0).forEach(cache => cache.clear());
});

describe("observed workload health consumer", () => {
  it("test_renders_available_partial_stale_and_unavailable_observation_states", async () => {
    route({
      views: {
        [WORKLOAD_ID]: view(),
        [PARTIAL_ID]: view({
          workload_id: PARTIAL_ID,
          observation: payload({
            status: "progressing",
            coverage: {
              complete: false,
              missing_sources: ["deployment_status"],
            },
            reasons: ["rollout_incomplete"],
          }),
        }),
        [STALE_ID]: view({
          workload_id: STALE_ID,
          freshness: "stale",
          observation: payload({
            status: "degraded",
            reasons: ["progress_deadline_exceeded"],
          }),
        }),
        [UNAVAILABLE_ID]: view({
          workload_id: UNAVAILABLE_ID,
          availability: "unavailable",
          freshness: "unavailable",
          observation: null,
          observed_at: null,
          last_attempt_at: "2026-10-26T10:02:00Z",
          last_attempt_state: "failed",
          last_attempt_reason: "cluster_unavailable",
        }),
      },
    });
    mount(
      <>
        <Observations id={WORKLOAD_ID} canMutate={false} />
        <Observations id={PARTIAL_ID} canMutate={false} />
        <Observations id={STALE_ID} canMutate={false} />
        <Observations id={UNAVAILABLE_ID} canMutate={false} />
      </>,
    );

    expect(await screen.findByText("Healthy")).toBeTruthy();
    const [healthy, partial, stale, unavailable] = await screen.findAllByRole(
      "region",
      { name: "Observed workload health" },
    );
    expect(within(healthy).getByText("Healthy")).toBeTruthy();
    expect(within(healthy).getByText("Fresh")).toBeTruthy();
    expect(within(healthy).getByText("Coverage complete")).toBeTruthy();
    expect(within(partial).getByText("Progressing")).toBeTruthy();
    expect(
      within(partial).getByText(
        "Coverage partial — missing: deployment_status",
      ),
    ).toBeTruthy();
    expect(within(stale).getByText("Degraded")).toBeTruthy();
    expect(within(stale).getByText("Stale")).toBeTruthy();
    expect(within(unavailable).getByText("Observation unavailable")).toBeTruthy();
    expect(
      within(unavailable).getByText(/cluster_unavailable/),
    ).toBeTruthy();
  });

  it("test_observation_polling_re_reads_the_projection", async () => {
    vi.useFakeTimers();
    try {
      let reads = 0;
      fixtureHttp.fetch.mockImplementation(async request => {
        const url = new URL((request as Request).url);
        if (url.pathname === `/api/workloads/${WORKLOAD_ID}/observation`) {
          reads += 1;
          return Response.json(view());
        }
        if (url.pathname === `/api/workloads/${WORKLOAD_ID}/observations`)
          return Response.json({ items: [], next_offset: null });
        if (url.pathname === "/api/incidents")
          return Response.json({ items: [], next_offset: null });
        return Response.json(
          { error: { code: "not_found", message: "Not found." } },
          { status: 404 },
        );
      });

      mount(<Observations id={WORKLOAD_ID} canMutate={false} />);
      await vi.advanceTimersByTimeAsync(10);
      expect(reads).toBe(1);
      // The 60-second stale policy is only visible if the projection is re-read.
      await vi.advanceTimersByTimeAsync(20_100);
      expect(reads).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("test_failed_refresh_keeps_prior_values_labeled_historical", async () => {
    route({
      views: {
        [WORKLOAD_ID]: view({
          freshness: "stale",
          observed_at: "2026-10-26T10:00:00Z",
          last_attempt_at: "2026-10-26T10:01:00Z",
          last_attempt_state: "failed",
          last_attempt_reason: "timeout",
          pending_refresh: true,
        }),
      },
    });
    mount(<Observations id={WORKLOAD_ID} canMutate={false} />);

    expect(await screen.findByText("Healthy")).toBeTruthy();
    expect(screen.getByText(/historical/i)).toBeTruthy();
    expect(screen.getByText(/Failed/)).toBeTruthy();
    expect(screen.getByText(/timeout/)).toBeTruthy();
    expect(screen.queryByText("Fresh")).toBeNull();
  });

  it("test_fixture_never_claims_live_health", async () => {
    route({
      views: {
        [WORKLOAD_ID]: view({
          source: "development_fixture",
          availability: "unavailable",
          freshness: "unavailable",
          observation: null,
          observed_at: null,
          last_attempt_at: null,
          last_attempt_state: null,
        }),
      },
    });
    mount(<Observations id={WORKLOAD_ID} canMutate />);

    expect(
      await screen.findByText("Development fixture · live health unavailable"),
    ).toBeTruthy();
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Request observation refresh" }),
    ).toBeNull();
  });

  it("test_refresh_is_operator_only_and_refreshes_projection", async () => {
    let requested = false;
    let reads = 0;
    fixtureHttp.fetch.mockImplementation(async request => {
      const req = request as Request;
      const url = new URL(req.url);
      if (
        url.pathname === `/api/workloads/${WORKLOAD_ID}/observation` &&
        req.method === "GET"
      ) {
        reads += 1;
        return Response.json(view({ pending_refresh: requested }));
      }
      if (
        url.pathname === `/api/workloads/${WORKLOAD_ID}/observation-refresh` &&
        req.method === "POST"
      ) {
        requested = true;
        return Response.json(
          {
            workload_id: WORKLOAD_ID,
            status: "pending",
            requested_at: "2026-10-26T10:01:00Z",
          },
          { status: 202 },
        );
      }
      if (url.pathname === `/api/workloads/${WORKLOAD_ID}/observations`)
        return Response.json({ items: [], next_offset: null });
      if (url.pathname === "/api/incidents")
        return Response.json({ items: [], next_offset: null });
      return Response.json(
        { error: { code: "not_found", message: "Not found." } },
        { status: 404 },
      );
    });

    const viewer = mount(<Observations id={WORKLOAD_ID} canMutate={false} />);
    await screen.findByText("Healthy");
    expect(
      screen.queryByRole("button", { name: "Request observation refresh" }),
    ).toBeNull();
    viewer.unmount();

    mount(<Observations id={WORKLOAD_ID} canMutate />);
    await screen.findByText("Healthy");
    await userEvent
      .setup()
      .click(
        screen.getByRole("button", { name: "Request observation refresh" }),
      );
    expect(
      await screen.findByText("Refresh pending · waiting for the observer."),
    ).toBeTruthy();
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(3));
  });

  it("test_related_incidents_query_includes_workload_uuid", async () => {
    route({
      views: { [WORKLOAD_ID]: view() },
      incidents: { [WORKLOAD_ID]: incidentPage() },
    });
    mount(<Observations id={WORKLOAD_ID} canMutate={false} />);

    expect(await screen.findByText("Checkout latency")).toBeTruthy();
    const request = fixtureHttp.fetch.mock.calls
      .map(([input]) => input as Request)
      .find(item => new URL(item.url).pathname === "/api/incidents");
    expect(request).toBeTruthy();
    expect(new URL(request!.url).searchParams.get("workload_uuid")).toBe(
      WORKLOAD_ID,
    );
  });

  it("test_incident_navigation_uses_exact_related_incident", async () => {
    route({
      views: { [WORKLOAD_ID]: view() },
      incidents: { [WORKLOAD_ID]: incidentPage() },
    });
    mount(<Observations id={WORKLOAD_ID} canMutate={false} />);

    const link = await screen.findByRole("link", {
      name: /Checkout latency/,
    });
    expect(link.getAttribute("href")).toBe(`#/incidents/${INCIDENT_ID}`);
    expect(link.getAttribute("href")).not.toContain(WORKLOAD_ID);
  });
});
