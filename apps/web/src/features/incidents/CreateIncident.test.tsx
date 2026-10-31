// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { workloadFixture } from "@/features/workloads/fixtures";
import { CreateIncident } from "./CreateIncident";

const http = vi.hoisted(() => ({ fetch: vi.fn<typeof fetch>() }));
vi.mock("@/lib/api/client", async original => {
  const module = await original<typeof import("@/lib/api/client")>();
  return { ...module, api: module.createTransport({ baseUrl: "http://localhost", fetch: http.fetch }).client };
});
const caches: QueryClient[] = [];
afterEach(() => { cleanup(); caches.splice(0).forEach(c => c.clear()); http.fetch.mockReset(); });

function mount(active = true) {
  const selected = { ...workloadFixture.items[0], id: "00000000-0000-4000-8000-000000000099", name: "page-two", active };
  const onCreated = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  http.fetch.mockImplementation(async input => {
    const request = input as Request;
    const url = new URL(request.url);
    if (request.method === "POST") {
      expect((await request.json()).workload_id).toBe(selected.id);
      return Response.json({ incident_id: selected.id, run_id: selected.id }, { status: 201 });
    }
    if (url.pathname.endsWith(selected.id)) return Response.json(selected);
    if (url.searchParams.get("offset") === "25") return Response.json({ items: [selected], next_offset: null });
    return Response.json({ ...workloadFixture, next_offset: 25 });
  });
  render(<QueryClientProvider client={cache}><CreateIncident initialWorkload={selected.id} onCreated={onCreated} /></QueryClientProvider>);
  return { selected, onCreated };
}

describe("exact workload incident admission", () => {
  it("preloads an exact identity absent from page one and preserves it while paging", async () => {
    const { selected, onCreated } = mount();
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Configured workload");
    await screen.findByRole("option", { name: "demo/page-two · fixture" });
    expect((select as HTMLSelectElement).value).toBe(selected.id);
    await user.click(screen.getByRole("button", { name: "Next workloads" }));
    expect((select as HTMLSelectElement).value).toBe(selected.id);
    await user.type(screen.getByLabelText("Incident title"), "Keep selected identity");
    await user.click(screen.getByRole("button", { name: "Create incident and collect evidence" }));
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(selected.id, selected.id));
  });
  it("blocks a retired preselected identity without substituting the first option", async () => {
    mount(false);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Create incident and collect evidence" }) as HTMLButtonElement).disabled).toBe(true);
    expect(http.fetch.mock.calls.every(([request]) => (request as Request).method === "GET")).toBe(true);
  });
});
