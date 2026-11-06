// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { CancelRun, CollectionProgress } from "./RunProgress";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async (original) => {
  const module = await original<typeof import("@/lib/api/client")>();
  return {
    ...module,
    api: module.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client,
  };
});
afterEach(() => {
  cleanup();
  http.fetch.mockReset();
});

type Run = components["schemas"]["RunView"];
const incidentId = "00000000-0000-4000-8000-000000000010";
function run(overrides: Partial<Run> = {}): Run {
  return {
    id: "00000000-0000-4000-8000-000000000020",
    incident_id: incidentId,
    state: "queued",
    reason: "Waiting for a collection worker.",
    created_at: "2026-11-01T00:00:00Z",
    finished_at: null,
    requested_by: null,
    origin: { kind: "operator", source_id: null },
    question: "Why?",
    question_source: "operator",
    window: null,
    queue_reason: null,
    outcome_code: null,
    pack_coverage: null,
    evidence: [],
    attempts: [],
    progress: {
      sources_total: 12,
      sources_committed: 0,
      sources_reused: 0,
      finalized: false,
      attempt: null,
      max_attempts: 3,
    },
    dispatch: { status: "allowed", reason: null },
    cancel: { allowed: true, reason: "queued" },
    diagnosis: "unavailable",
    recovery: "unavailable",
    ...overrides,
  };
}

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function posts() {
  return http.fetch.mock.calls.filter(([input]) => (input as Request).method === "POST");
}

describe("collection progress", () => {
  it("shows persisted checkpoint progress and resumption after worker loss", () => {
    mount(
      <CollectionProgress
        run={run({
          state: "collecting",
          progress: {
            sources_total: 12,
            sources_committed: 3,
            sources_reused: 3,
            finalized: false,
            attempt: 2,
            max_attempts: 3,
          },
        })}
      />,
    );
    expect(
      screen.getByText(
        "3 of 12 catalog sources saved · Attempt 2 of 3 · 3 reused from an earlier attempt",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/Saved sources survive a worker restart/)).toBeTruthy();
  });

  it("explains a trusted dispatch pause without offering a fake override", () => {
    mount(
      <CollectionProgress run={run({ dispatch: { status: "paused", reason: "recovery_mode" } })} />,
    );
    expect(screen.getByRole("status").textContent).toBe(
      "Dispatch is paused: trusted recovery mode. This request stays queued and starts only after a trusted host operator resumes collection.",
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers explicit manual reinvestigation after a failed collection", async () => {
    const onReinvestigate = vi.fn();
    mount(
      <CollectionProgress
        run={run({
          state: "failed",
          dispatch: null,
          attempts: [1, 2, 3].map((number) => ({
            number,
            state: "expired" as const,
            started_at: "2026-11-01T00:00:00Z",
            finished_at: "2026-11-01T00:00:30Z",
            error: "Worker lease expired; collection may be retried.",
            sources_committed: 0,
          })),
        })}
        onReinvestigate={onReinvestigate}
      />,
    );
    expect(screen.getByText(/Collection stopped after 3 attempts/)).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: "Request a new investigation" }));
    expect(onReinvestigate).toHaveBeenCalledOnce();
  });

  it("labels declared analysis states honestly", () => {
    mount(<CollectionProgress run={run({ state: "waiting_for_quota" })} />);
    expect(screen.getByText(/AI analysis is unavailable in this release/)).toBeTruthy();
  });

  it("says when an earlier run did not capture progress", () => {
    mount(<CollectionProgress run={run({ state: "insufficient_evidence", progress: null })} />);
    expect(screen.getByText("Collection progress was not captured for this earlier run.")).toBeTruthy();
  });
});

describe("safe cancellation", () => {
  it("disables cancel with the server reason once collection started", () => {
    mount(
      <CancelRun
        incidentId={incidentId}
        run={run({ state: "collecting", cancel: { allowed: false, reason: "collection_started" } })}
        canMutate
      />,
    );
    const button = screen.getByRole("button", { name: "Cancel investigation" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    const reason = document.getElementById(button.getAttribute("aria-describedby")!);
    expect(reason?.textContent).toMatch(/Collection already started, so it cannot be cancelled/);
  });

  it("shows a Viewer the boundary without any mutation control", () => {
    mount(<CancelRun incidentId={incidentId} run={run()} canMutate={false} />);
    expect(screen.getByText(/This request has not started/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("moves focus through confirm, keep and the saved outcome", async () => {
    http.fetch.mockImplementation(async () => Response.json(run({ state: "cancelled" })));
    const user = userEvent.setup();
    mount(<CancelRun incidentId={incidentId} run={run()} canMutate />);
    await user.click(screen.getByRole("button", { name: "Cancel investigation" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Confirm cancellation" }));
    expect(posts()).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Keep it" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel investigation" }));
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Confirm cancellation" }));
    const outcome = await screen.findByText("Investigation cancelled. No further attempt will start.");
    expect(document.activeElement).toBe(outcome);
    expect(posts()).toHaveLength(1);
  });

  it("reports a claim that won the race honestly and refreshes the Run", async () => {
    http.fetch.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === "POST")
        return Response.json(
          {
            error: {
              code: "run_not_cancellable",
              message: "Collection already started; committed sources remain part of the history.",
            },
          },
          { status: 409 },
        );
      return Response.json({ items: [], next_offset: null });
    });
    const user = userEvent.setup();
    mount(<CancelRun incidentId={incidentId} run={run()} canMutate />);
    await user.click(screen.getByRole("button", { name: "Cancel investigation" }));
    await user.click(screen.getByRole("button", { name: "Confirm cancellation" }));
    const outcome = await screen.findByText(
      "The investigation started before the cancellation; it was not cancelled.",
    );
    expect(document.activeElement).toBe(outcome);
    expect(screen.queryByText(/Unable to load or save/)).toBeNull();
    expect(posts()).toHaveLength(1);
  });

  it("never replays a cancellation after the session expires", async () => {
    http.fetch.mockImplementation(async () =>
      Response.json(
        { error: { code: "session_expired", message: "Sign in again." } },
        { status: 401 },
      ),
    );
    const user = userEvent.setup();
    const view = mount(<CancelRun incidentId={incidentId} run={run()} canMutate />);
    await user.click(screen.getByRole("button", { name: "Cancel investigation" }));
    await user.click(screen.getByRole("button", { name: "Confirm cancellation" }));
    expect(await screen.findByText(/Unable to load or save/)).toBeTruthy();
    view.unmount();
    mount(<CancelRun incidentId={incidentId} run={run()} canMutate />);
    expect(posts()).toHaveLength(1);
  });
});
