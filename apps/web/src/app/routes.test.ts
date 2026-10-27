import { describe, expect, it } from "vitest";
import { safeRoute } from "./routes";

describe("safe return navigation", () => {
  const id = "74483c3e-adc2-4d05-93a6-c218afc6ebe7";
  it("retains an exact incident and selected run", () => {
    expect(safeRoute(`#/incidents/${id}?run=${id}`)).toEqual({
      page: "incidents",
      id,
      run: id,
    });
  });
  it("retains an exact workload identity for read-only navigation", () => {
    expect(safeRoute(`#/workloads?workload=${id}`)).toEqual({ page: "workloads", id, run: "" });
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
    expect(safeRoute(hash)).toEqual({ page: "queue", id: "", run: "" });
  });
});
