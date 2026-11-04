// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { buildIncidentHash, buildWorkQueueHash, incidentReturnHash, parseWorkQueueHash, readRoute, safeRoute } from "./routes";

describe("safe return navigation", () => {
  const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";
  it("retains an exact incident and selected run", () => {
    expect(safeRoute(`#/incidents/${id}?run=${id}`)).toEqual({
      page: "incidents",
      id,
      run: id,
      baseline: "",
    });
  });
  it("retains an exact workload identity for read-only navigation", () => {
    expect(safeRoute(`#/workloads?workload=${id}`)).toEqual({ page: "workloads", id, run: "", baseline: "" });
    expect(safeRoute(`#/workloads?workload=${id}&baseline=${id}`).baseline).toBe(id);
  });
  it.each(["queue", "workloads", "connections", "reports"])(
    "retains %s",
    (page) => {
      expect(safeRoute(`#/${page}`).page).toBe(page);
    },
  );
  it.each([
    "https://foreign.test",
    "#//foreign.test",
    "#/https://foreign.test",
    "#/unknown",
    "#/queue?next=https://foreign.test",
    "#/queue/extra",
    `#/incidents/${id}?token=secret`,
    "#/incidents/not-a-uuid",
    `#/incidents/${id}?run=raw-evidence`,
    `#/incidents/${id}?run=${id}&next=bad`,
    "#/workloads?workload=not-a-uuid",
    `#/workloads?workload=${id}&next=https://foreign.test`,
  ])("falls back safely for %s", (hash) => {
    expect(safeRoute(hash)).toEqual({ page: "queue", id: "", run: "", baseline: "" });
  });
});

describe("work queue URL state", () => {
  const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";

  it("parses search from the queue hash", () => {
    expect(parseWorkQueueHash("#/queue?search=timeout").search).toBe("timeout");
  });

  it("parses every whitelisted parameter", () => {
    const params = parseWorkQueueHash(
      `#/queue?search=db&workload=${id}&origin=alertmanager&state=open` +
        `&severity=critical&alert_signal=firing&sort_by=severity&sort_order=asc` +
        `&offset=25&selected=${id}&view=blocked&run_state=waiting_for_quota` +
        `&execution_class=terminal_unknown&anchor_created_at=2026-11-01T00%3A00%3A00.000Z&anchor_id=${id}`,
    );
    expect(params).toEqual({
      search: "db",
      workload: id,
      origin: "alertmanager",
      state: "open",
      severity: "critical",
      alert_signal: "firing",
      sort_by: "severity",
      sort_order: "asc",
      offset: 25,
      selected: id,
      view: "blocked",
      run_state: "waiting_for_quota",
      execution_class: "terminal_unknown",
      anchor_created_at: "2026-11-01T00:00:00.000Z",
      anchor_id: id,
    });
  });

  it("returns defaults for a bare queue hash", () => {
    expect(parseWorkQueueHash("#/queue")).toEqual({
      sort_by: "last_seen_at",
      sort_order: "desc",
      offset: 0,
    });
  });

  it("drops unknown keys and invalid values", () => {
    const params = parseWorkQueueHash(
      `#/queue?next=https://foreign.test&state=bogus&origin=unknown` +
        `&workload=not-a-uuid&selected=raw&offset=abc&sort_by=description` +
      `&sort_order=sideways&alert_signal=noise&severity=${"x".repeat(33)}` +
        "&view=unknown&run_state=unknown&execution_class=unknown" +
        "&anchor_created_at=not-a-date&anchor_id=raw",
    );
    expect(params).toEqual({ sort_by: "last_seen_at", sort_order: "desc", offset: 0 });
  });

  it("treats negative and non-integer offsets as the default", () => {
    expect(parseWorkQueueHash("#/queue?offset=-5").offset).toBe(0);
    expect(parseWorkQueueHash("#/queue?offset=2.5").offset).toBe(0);
  });

  it("builds a bare queue hash when no parameter is set", () => {
    expect(buildWorkQueueHash({})).toBe("#/queue");
  });

  it("omits default sort and zero offset", () => {
    expect(buildWorkQueueHash({ sort_by: "last_seen_at", sort_order: "desc", offset: 0 })).toBe(
      "#/queue",
    );
  });

  it("builds non-default parameters and round-trips them", () => {
    const params = {
      search: "test",
      state: "open" as const,
      alert_signal: "firing" as const,
      sort_by: "severity" as const,
      sort_order: "asc" as const,
      offset: 25,
    };
    const hash = buildWorkQueueHash(params);
    expect(hash).toContain("search=test");
    expect(hash).toContain("state=open");
    expect(hash).toContain("alert_signal=firing");
    expect(hash).toContain("sort_by=severity");
    expect(hash).toContain("sort_order=asc");
    expect(hash).toContain("offset=25");
    expect(parseWorkQueueHash(hash)).toEqual(params);
  });

  it("recognizes a queue hash with a query as the queue route", () => {
    expect(safeRoute("#/queue?search=timeout")).toEqual({
      page: "queue",
      id: "",
      run: "",
      baseline: "",
    });
  });

  it("retains microsecond anchors through build, parse and route canonicalization", () => {
    const anchor = "2026-11-01T10:00:00.123456Z";
    const hash = buildWorkQueueHash({ anchor_created_at: anchor, anchor_id: id, offset: 25 });
    expect(parseWorkQueueHash(hash).anchor_created_at).toBe(anchor);
    window.history.replaceState(null, "", `/${hash}`);
    readRoute();
    expect(parseWorkQueueHash(window.location.hash).anchor_created_at).toBe(anchor);
    expect(window.location.hash).toBe(hash);
  });

  it("keeps only safe queue return targets through incident canonicalization", () => {
    const queue = buildWorkQueueHash({ search: "db", offset: 25, selected: id });
    window.history.replaceState(null, "", `/${buildIncidentHash(id, id, queue)}`);
    expect(readRoute().page).toBe("incidents");
    expect(incidentReturnHash(window.location.hash)).toBe(queue);
    window.history.replaceState(null, "", `/#/incidents/${id}?return_to=${encodeURIComponent("https://foreign.test")}`);
    expect(readRoute().page).toBe("incidents");
    expect(incidentReturnHash(window.location.hash)).toBe("#/queue");
    expect(window.location.hash).toBe(`#/incidents/${id}`);
  });

  it("canonicalizes the queue hash by dropping foreign keys and invalid values", () => {
    window.history.replaceState(null, "", "/#/queue?state=open&next=https://foreign.test");
    expect(readRoute().page).toBe("queue");
    expect(window.location.hash).toBe("#/queue?state=open");

    window.history.replaceState(null, "", "/#/queue?state=bogus");
    readRoute();
    expect(window.location.hash).toBe("#/queue");
  });
});
