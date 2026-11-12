// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Knowledge } from "./Knowledge";
import { KnowledgeStatus } from "./KnowledgeStatus";
import {
  buildIncidentHash,
  buildKnowledgeHash,
  safeRoute,
  type KnowledgeRoute,
} from "@/app/routes";
import { download } from "@/lib/download";
import {
  ids,
  documentFixture,
  chunkFixture,
  pageFixture,
  refFixture,
  statusFixture,
} from "./fixtures";

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
vi.mock("@/lib/download", () => ({ download: vi.fn() }));
const caches: QueryClient[] = [];
function mount(route: KnowledgeRoute = {}) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  caches.push(cache);
  const view = (value: KnowledgeRoute) => (
    <QueryClientProvider client={cache}>
      <Knowledge route={value} />
    </QueryClientProvider>
  );
  return { ...render(view(route)), cache, view };
}
function response(request: Request) {
  const url = new URL(request.url);
  if (url.pathname.endsWith("/index-status"))
    return Response.json(statusFixture);
  if (url.pathname === "/api/knowledge/documents")
    return Response.json(pageFixture);
  if (url.pathname === `/api/knowledge/documents/${ids.document}`)
    return Response.json(documentFixture);
  if (url.pathname === `/api/knowledge/chunks/${ids.chunk}`)
    return Response.json(chunkFixture);
  if (url.pathname.includes("/selections/"))
    return Response.json({
      reference: refFixture,
      document: documentFixture,
      chunk: chunkFixture,
      selection_id: ids.selection,
    });
  throw new Error(`Unexpected fixture route: ${url.pathname}`);
}
beforeEach(() => {
  http.fetch.mockImplementation(async (input) => response(input as Request));
});
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((cache) => cache.clear());
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

it("preserves catalog filters in exact document links and keeps all catalog actions read-only", async () => {
  mount({ search: "memory", tags: ["oom"] });
  const link = await screen.findByRole("link", { name: documentFixture.title });
  expect(link.getAttribute("href")).toBe(
    buildKnowledgeHash({
      search: "memory",
      tags: ["oom"],
      document: ids.document,
    }),
  );
  expect(
    (screen.getByLabelText("Search guidance") as HTMLInputElement).value,
  ).toBe("memory");
  expect(
    (screen.getByLabelText("Tags (comma-separated)") as HTMLInputElement).value,
  ).toBe("oom");
  expect(
    http.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});

it("renders hostile Markdown as inert exact text and downloads unchanged bytes plus provenance", async () => {
  mount({ document: ids.document });
  await screen.findByRole("button", { name: "Download exact document" });
  expect(
    document.querySelectorAll(".knowledge-reader img, .knowledge-reader script")
      .length,
  ).toBe(0);
  expect(document.querySelector("a[href^='javascript:']")).toBeNull();
  expect(document.querySelector(".knowledge-text")!.textContent).toBe(
    documentFixture.content,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Download exact document" }),
  );
  expect(download).toHaveBeenCalledWith(
    documentFixture.content,
    "memory-guide-v2.md",
    "text/markdown;charset=utf-8",
  );
  await user.click(screen.getByRole("button", { name: "Download provenance" }));
  const exported = JSON.parse(vi.mocked(download).mock.calls[1][0]);
  expect(exported.id).toBe(ids.document);
  expect(exported.content_sha256).toBe(documentFixture.content_sha256);
  expect(exported.content).toBeUndefined();
});

it("uses the Run-scoped resolution endpoint, focuses the exact chunk and returns to the same Run context", async () => {
  const returnTo = buildIncidentHash(
    ids.incident,
    ids.run,
    "#/queue?search=memory",
    ids.document,
  );
  mount({
    document: ids.document,
    chunk: ids.chunk,
    incident: ids.incident,
    run: ids.run,
    selection: ids.selection,
    returnTo,
  });
  const heading = await screen.findByRole("heading", {
    name: "Exact stored chunk",
  });
  await waitFor(() => expect(document.activeElement).toBe(heading));
  expect(
    screen
      .getByRole("link", { name: "← Back to selected Run" })
      .getAttribute("href"),
  ).toBe(returnTo);
  const requests = http.fetch.mock.calls.map(
    ([request]) => new URL((request as Request).url),
  );
  expect(
    requests.some(
      (url) =>
        url.pathname.includes(
          `/selections/${ids.selection}/chunks/${ids.chunk}`,
        ) && url.searchParams.get("document_version_id") === ids.document,
    ),
  ).toBe(true);
  expect(
    requests.some(
      (url) => url.pathname === `/api/knowledge/documents/${ids.document}`,
    ),
  ).toBe(false);
});

it("keeps a retired exact version readable and retries the same ID after a transient read failure", async () => {
  let fail = true;
  http.fetch.mockImplementation(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname === `/api/knowledge/documents/${ids.document}` && fail)
      return Response.json(
        {
          error: {
            code: "database_unavailable",
            message: "Retry this exact read.",
          },
        },
        { status: 503 },
      );
    if (
      url.pathname === "/api/knowledge/documents" &&
      url.searchParams.get("limit") === "100"
    )
      return Response.json({ ...pageFixture, items: [] });
    return response(request);
  });
  mount({ document: ids.document, search: "memory", tags: ["oom"] });
  await screen.findByText(
    "The exact requested version remains selected. No current version is substituted.",
  );
  fail = false;
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText(
    "Historical version · retired from the current catalog",
  );
  expect(
    (screen.getByLabelText("Search guidance") as HTMLInputElement).value,
  ).toBe("memory");
  expect(
    screen
      .getByRole("link", { name: "← Catalog with these filters" })
      .getAttribute("href"),
  ).toBe(buildKnowledgeHash({ search: "memory", tags: ["oom"] }));
});

it("distinguishes empty filters from an unindexed catalog", async () => {
  http.fetch.mockImplementation(async (input) =>
    new URL((input as Request).url).pathname === "/api/knowledge/documents"
      ? Response.json({
          ...pageFixture,
          items: [],
          catalog_empty_reason: "no_matches",
        })
      : response(input as Request),
  );
  const mounted = mount({ search: "absent" });
  await screen.findByText("No runbooks match these filters.");
  http.fetch.mockImplementation(async (input) =>
    new URL((input as Request).url).pathname === "/api/knowledge/documents"
      ? Response.json({
          ...pageFixture,
          items: [],
          has_active_generation: false,
          generation_id: null,
          source_digest: null,
          catalog_empty_reason: "not_indexed",
        })
      : response(input as Request),
  );
  await act(async () => {
    await mounted.cache.invalidateQueries({ queryKey: ["knowledge"] });
  });
  await screen.findByText("No catalog has been indexed yet.");
});

it("writes bounded filters to the safe route and preserves them across a reload parse", async () => {
  mount();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Search guidance"), "memory pressure");
  await user.type(
    screen.getByLabelText("Tags (comma-separated)"),
    "oom, memory, oom",
  );
  await user.click(screen.getByRole("button", { name: "Search" }));
  expect(safeRoute(window.location.hash)).toMatchObject({
    page: "knowledge",
    knowledge: { search: "memory pressure", tags: ["oom", "memory"] },
  });
});

it("shows a degraded last-good index without implying unavailable historical content", async () => {
  http.fetch.mockResolvedValue(
    Response.json({ ...statusFixture, state: "degraded" }),
  );
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  caches.push(cache);
  render(
    <QueryClientProvider client={cache}>
      <KnowledgeStatus />
    </QueryClientProvider>,
  );
  await screen.findByText(
    /last successful catalog and historical references remain readable/,
  );
  expect(
    screen.getByText("Full-text catalog · Semantic retrieval not configured"),
  ).toBeTruthy();
  expect(
    http.fetch.mock.calls.every(([r]) => (r as Request).method === "GET"),
  ).toBe(true);
});
