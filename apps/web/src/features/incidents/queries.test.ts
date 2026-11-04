import { describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { createTransport } from "@/lib/api/client";
import { incidentQueryOptions } from "./queries";

const page = { items: [], next_offset: null };
const workload = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";

function mount(filters: Parameters<typeof incidentQueryOptions>[1] = {}) {
  const urls: string[] = [];
  const transport = createTransport({
    baseUrl: "http://localhost",
    fetch: async (request) => {
      urls.push(String((request as Request).url));
      return Response.json(page);
    },
  });
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return { urls, cache, transport };
}

describe("incident query filters", () => {
  it("sends only the paging parameters when no filter is set", async () => {
    const { urls, cache, transport } = mount();
    await cache.fetchQuery(incidentQueryOptions(0, {}, transport.client));
    expect(urls[0]).toBe("http://localhost/api/incidents?limit=25&offset=0");
    cache.clear();
  });

  it("sends every defined filter and sort parameter", async () => {
    const { urls, cache, transport } = mount();
    const filters = {
      search: "db",
      workload_id: workload,
      origin: "alertmanager" as const,
      state: "open" as const,
      severity: "critical",
      alert_signal: "firing" as const,
      sort_by: "severity" as const,
      sort_order: "asc" as const,
    };
    await cache.fetchQuery(incidentQueryOptions(25, filters, transport.client));
    const url = new URL(urls[0]);
    expect(url.pathname).toBe("/api/incidents");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "25",
      offset: "25",
      search: "db",
      workload_id: workload,
      origin: "alertmanager",
      state: "open",
      severity: "critical",
      alert_signal: "firing",
      sort_by: "severity",
      sort_order: "asc",
    });
    cache.clear();
  });

  it("keeps offset and filters in the query key", async () => {
    const { cache } = mount();
    const first = incidentQueryOptions(0, { state: "open" });
    const second = incidentQueryOptions(0, { state: "resolved" });
    expect(first.queryKey).not.toEqual(second.queryKey);
    expect(first.queryKey).toContain("page");
    cache.clear();
  });
});
