// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RunGuidance } from "./RunGuidance";
import { ids, savedFixture } from "./fixtures";
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
function mount(canMutate = true) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 3 } },
  });
  caches.push(cache);
  return render(
    <QueryClientProvider client={cache}>
      <RunGuidance
        incidentId={ids.incident}
        runId={ids.run}
        canMutate={canMutate}
      />
    </QueryClientProvider>,
  );
}
beforeEach(() =>
  http.fetch.mockImplementation(async () =>
    Response.json({ state: "not_selected", selection: null, reason: null }),
  ),
);
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((cache) => cache.clear());
  http.fetch.mockReset();
  window.history.replaceState(null, "", "/");
});

it("does not mutate on mount and keeps Viewer controls read-only", async () => {
  mount(false);
  await screen.findByText("No guidance has been selected for this Run.");
  expect(screen.queryByRole("button", { name: "Find guidance" })).toBeNull();
  expect(
    http.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});

it("creates only after explicit action, saves the acknowledgement and links exact Run membership", async () => {
  let finish!: (response: Response) => void;
  http.fetch.mockImplementation(async (input) =>
    (input as Request).method === "POST"
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : Response.json({ state: "not_selected", selection: null, reason: null }),
  );
  window.location.hash = `#/incidents/${ids.incident}?run=${ids.run}`;
  mount();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: "Find guidance" }),
  );
  expect(
    (
      screen.getByRole("button", {
        name: "Finding guidance…",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  await act(async () => finish(Response.json(savedFixture)));
  await screen.findByText("Saved manual selection");
  const link = screen.getByRole("link", {
    name: /Synthetic memory guide · version 2 · result 1/,
  });
  expect(safeRoute(link.getAttribute("href")!).knowledge).toMatchObject({
    document: ids.document,
    chunk: ids.chunk,
    incident: ids.incident,
    run: ids.run,
    selection: ids.selection,
  });
  expect(screen.queryByRole("button", { name: "Find guidance" })).toBeNull();
  expect(
    http.fetch.mock.calls.filter(([r]) => (r as Request).method === "POST"),
  ).toHaveLength(1);
});

it("never automatically replays a failed mutation and offers explicit retry", async () => {
  let posts = 0;
  http.fetch.mockImplementation(async (input) => {
    if ((input as Request).method !== "POST")
      return Response.json({ state: "not_selected", selection: null });
    posts++;
    return posts === 1
      ? Response.json(
          {
            error: {
              code: "database_unavailable",
              message: "Retry explicitly.",
            },
          },
          { status: 503 },
        )
      : Response.json(savedFixture);
  });
  mount();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: "Find guidance" }),
  );
  await screen.findByRole("button", { name: "Retry Find guidance" });
  await waitFor(() => expect(posts).toBe(1));
  await user.click(screen.getByRole("button", { name: "Retry Find guidance" }));
  await screen.findByText("Saved manual selection");
  expect(posts).toBe(2);
});

it("retains an acknowledged no-match as a saved selection for either role", async () => {
  http.fetch.mockImplementation(async () =>
    Response.json({
      ...savedFixture,
      selection: {
        ...savedFixture.selection,
        retrieval: {
          ...savedFixture.selection.retrieval,
          status: "no_matches",
          chunks: [],
          content_bytes: 0,
        },
      },
    }),
  );
  mount(false);
  await screen.findByText(
    "No matching guidance was found for this saved request.",
  );
  expect(screen.queryByRole("button", { name: /Find guidance/ })).toBeNull();
  expect(screen.getByText(/A new investigation is needed/)).toBeTruthy();
});

it("keeps legacy request gaps explicit without showing a creation control", async () => {
  http.fetch.mockImplementation(async () =>
    Response.json({
      state: "unavailable",
      reason: "legacy_request",
      selection: null,
    }),
  );
  mount();
  await screen.findByText(/original question or scope was not captured/);
  expect(screen.queryByRole("button", { name: /Find guidance/ })).toBeNull();
});

it("distinguishes an unavailable index response and allows a later explicit retry", async () => {
  http.fetch.mockImplementation(async (input) =>
    Response.json(
      (input as Request).method === "POST"
        ? { state: "unavailable", reason: "not_indexed", selection: null }
        : { state: "not_selected", selection: null },
    ),
  );
  mount();
  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "Find guidance" }));
  await screen.findByText(/index is not ready for guidance/);
  expect(
    screen.getByRole("button", { name: "Retry Find guidance" }),
  ).toBeTruthy();
});
