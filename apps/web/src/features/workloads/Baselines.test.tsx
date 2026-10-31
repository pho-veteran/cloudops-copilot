// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Baselines } from "./Baselines";

const mocks = vi.hoisted(() => ({ GET: vi.fn(), POST: vi.fn() }));
vi.mock("@/lib/api/client", () => ({ api: mocks, unwrap: (value: unknown) => value, message: (error: Error) => error.message }));
const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";
const baseline = "94483c3e-adc2-4d05-93a6-c218afc6ebe7";
const candidate = {
  id, digest: "a".repeat(64), snapshot: { profile: "deployment-images-resources/v1" },
  server_time: "2026-11-01T00:00:00Z", confirm_after: "2026-10-31T23:59:59Z", expires_at: "2026-11-01T00:05:00Z",
};
function mount(canMutate = true, selectedBaseline?: string) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={cache}><Baselines id={id} canMutate={canMutate} selectedBaseline={selectedBaseline} /></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.resetAllMocks(); window.location.hash = ""; });
function responses() {
  mocks.GET.mockImplementation(async (path: string) => {
    if (path.endsWith("baseline-candidate")) return candidate;
    if (path.endsWith("baselines")) return { items: [], next_cursor: null };
    return { state: "manual_only", reason: "missing", execution_eligible: false, limitations: "No execution authority." };
  });
}

describe("baseline preparation and explicit selection", () => {
  it("refetches stale selected context when returning to a cached selection and labels response time", async () => {
    responses();
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (selected: string) => <QueryClientProvider client={cache}><Baselines id={id} canMutate selectedBaseline={selected} /></QueryClientProvider>;
    const view = render(tree(baseline));
    await screen.findByText(/Context response received/);
    cache.setQueryData(["recovery-context", id, baseline], cache.getQueryData(["recovery-context", id, baseline]), { updatedAt: Date.now() - 31_000 });
    view.rerender(tree(id));
    await waitFor(() => expect(mocks.GET.mock.calls.some(([path, options]) => path.endsWith("recovery-context") && options.params.query.baseline_id === id)).toBe(true));
    view.rerender(tree(baseline));
    await waitFor(() => expect(mocks.GET.mock.calls.filter(([path, options]) => path.endsWith("recovery-context") && options.params.query.baseline_id === baseline)).toHaveLength(2));
  });
  it("starts without selecting the newest baseline and forwards an exact URL selection", async () => {
    responses(); mount(true, baseline);
    await waitFor(() => expect(mocks.GET).toHaveBeenCalledWith("/api/workloads/{workload_id}/recovery-context", { params: { path: { workload_id: id }, query: { baseline_id: baseline } } }));
    expect(screen.getByText(`Selected: ${baseline}`)).toBeTruthy();
    expect(window.location.hash).toBe("");
  });
  it("clears the old candidate when a fresh preview fails", async () => {
    responses(); mount();
    fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    await screen.findByText(/Candidate digest:/);
    mocks.GET.mockRejectedValueOnce(new Error("Fresh read unavailable"));
    fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    await waitFor(() => expect(screen.queryByText(/Candidate digest:/)).toBeNull());
    expect(screen.queryByText("Confirm unchanged healthy baseline")).toBeNull();
  });
  it("requires a fresh preview after failed confirmation and never retries the intent", async () => {
    responses(); mocks.POST.mockRejectedValue(new Error("Candidate changed")); mount();
    fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    fireEvent.click(await screen.findByText("Confirm unchanged healthy baseline"));
    await screen.findByText("Inspect a fresh candidate before trying again.");
    expect(screen.queryByText(/Candidate digest:/)).toBeNull();
    expect(mocks.POST).toHaveBeenCalledTimes(1);
  });
  it("allows Viewer inspection without confirmation", async () => {
    responses(); mount(false);
    fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    await screen.findByText("Viewer access: an Operator must inspect and confirm their own candidate.");
    expect(screen.queryByText("Confirm unchanged healthy baseline")).toBeNull();
    expect(mocks.POST).not.toHaveBeenCalled();
  });
  it("selects the exact confirmed response and clears the candidate", async () => {
    responses(); mocks.POST.mockResolvedValue({ id: baseline }); mount();
    fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    fireEvent.click(await screen.findByText("Confirm unchanged healthy baseline"));
    await waitFor(() => expect(window.location.hash).toBe(`#/workloads?workload=${id}&baseline=${baseline}`));
    expect(screen.queryByText(/Candidate digest:/)).toBeNull();
  });
  it("disables expired candidates without assuming the device clock", async () => {
    responses(); mocks.GET.mockImplementation(async (path: string) => path.endsWith("baseline-candidate") ? { ...candidate, expires_at: "2026-10-31T23:59:59Z" } : path.endsWith("baselines") ? { items: [] } : { state: "manual_only", reason: "missing" });
    mount(); fireEvent.click(screen.getByText("Inspect live baseline candidate"));
    await screen.findByText("Candidate expired. Inspect a fresh candidate.");
    expect(screen.queryByText("Confirm unchanged healthy baseline")).toBeNull();
  });
});
