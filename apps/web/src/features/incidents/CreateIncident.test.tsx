// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { workloadFixture } from "@/features/workloads/fixtures";
import { clearRequestKeys } from "@/features/investigations/request";
import { CreateIncident } from "./CreateIncident";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async original => {
  const module = await original<typeof import("@/lib/api/client")>();
  return { ...module, api: module.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client };
});
const caches: QueryClient[] = [];
afterEach(() => { cleanup(); caches.splice(0).forEach(c => c.clear()); http.fetch.mockReset(); clearRequestKeys(); });

const admission = (id: string) => ({
  incident_id: id,
  run_id: id,
  state: "queued",
  queue_reason: null,
  window: { start: "2026-11-01T00:00:00Z", end: "2026-11-01T00:30:00Z", source: "preset_30" },
});

function mount(active = true, post?: (request: Request) => Promise<Response>) {
  const selected = { ...workloadFixture.items[0], id: "00000000-0000-4000-8000-000000000099", name: "page-two", active };
  const onCreated = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  const posts: { key: string | null; body: Record<string, unknown> }[] = [];
  http.fetch.mockImplementation(async input => {
    const request = input as Request;
    const url = new URL(request.url);
    if (request.method === "POST") {
      const body = await request.clone().json();
      expect(body.workload_id).toBe(selected.id);
      posts.push({ key: request.headers.get("idempotency-key"), body });
      return post ? post(request) : Response.json(admission(selected.id), { status: 201 });
    }
    if (url.pathname.endsWith(selected.id)) return Response.json(selected);
    if (url.searchParams.get("offset") === "25") return Response.json({ items: [selected], next_offset: null });
    return Response.json({ ...workloadFixture, next_offset: 25 });
  });
  const view = render(<QueryClientProvider client={cache}><CreateIncident initialWorkload={selected.id} onCreated={onCreated} /></QueryClientProvider>);
  return { selected, onCreated, posts, view, cache };
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Incident title"), "Keep selected identity");
  await user.type(screen.getByLabelText("Investigation question"), "Why are pods restarting?");
}

describe("exact workload incident admission", () => {
  it("preloads an exact identity absent from page one and preserves it while paging", async () => {
    const { selected, onCreated, posts } = mount();
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Configured workload");
    await screen.findByRole("option", { name: "demo/page-two · fixture" });
    expect((select as HTMLSelectElement).value).toBe(selected.id);
    await user.click(screen.getByRole("button", { name: "Next workloads" }));
    expect((select as HTMLSelectElement).value).toBe(selected.id);
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create incident and collect evidence" }));
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(selected.id, selected.id));
    expect(posts[0].body).toMatchObject({ question: "Why are pods restarting?", window: { preset: 30 } });
  });
  it("blocks a retired preselected identity without substituting the first option", async () => {
    mount(false);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Create incident and collect evidence" }) as HTMLButtonElement).disabled).toBe(true);
    expect(http.fetch.mock.calls.every(([request]) => (request as Request).method === "GET")).toBe(true);
  });
  it("sends a chosen preset and blocks an invalid custom interval before any request", async () => {
    const { posts } = mount();
    const user = userEvent.setup();
    await fill(user);
    const choice = screen.getByLabelText("Investigate");
    await user.selectOptions(choice, "explicit");
    const end = screen.getByLabelText("End (your local time)") as HTMLInputElement;
    await user.clear(end);
    await user.type(end, "2999-01-01T00:00");
    expect((await screen.findByRole("alert")).textContent).toContain("cannot be in the future");
    const submit = screen.getByRole("button", { name: "Create incident and collect evidence" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.selectOptions(choice, "15");
    await user.click(submit);
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body.window).toEqual({ preset: 15 });
  });
  it("reuses the same key for an uncertain retry after the form remounts", async () => {
    let attempt = 0;
    const { posts, view, cache, selected } = mount(true, async () => {
      attempt++;
      if (attempt === 1) throw new TypeError("network lost");
      return Response.json(admission(selected.id), { status: 201 });
    });
    const user = userEvent.setup();
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create incident and collect evidence" }));
    await screen.findByText(/Cannot reach the local API/);
    view.unmount();
    const onCreated = vi.fn();
    render(<QueryClientProvider client={cache}><CreateIncident initialWorkload={selected.id} onCreated={onCreated} /></QueryClientProvider>);
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create incident and collect evidence" }));
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(posts).toHaveLength(2);
    expect(posts[1].key).toBe(posts[0].key);
  });
});
