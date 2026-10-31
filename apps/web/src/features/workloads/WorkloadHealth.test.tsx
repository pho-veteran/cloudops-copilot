// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { components } from "@/lib/api/generated/schema";
import { WorkloadHealth } from "./WorkloadHealth";

const summary: components["schemas"]["ObservationSummary"] = {
  evaluated_at: "2026-10-27T00:00:00Z", effective_health: "healthy", freshness: "fresh",
  coverage: "complete", age_seconds: 59, stale_after_seconds: 60,
  desired: 1, ready: 1, reasons: [], refreshing: false,
};

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("freshness-aware health", () => {
  it("downgrades a cached positive badge without another HTTP response", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "performance"] });
    render(<WorkloadHealth summary={summary} />);
    expect(screen.getByText("Kubernetes: healthy")).toBeTruthy();
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.getByText("Kubernetes: unknown")).toBeTruthy();
    expect(screen.getByText("Freshness: stale")).toBeTruthy();
    expect(screen.getByText(/Readiness: Unknown/)).toBeTruthy();
  });
  it("drops positive health when a pending refresh exceeds its server deadline", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "performance"] });
    render(<WorkloadHealth summary={{ ...summary, age_seconds: 1, refreshing: true, refresh_remaining_seconds: 1 }} />);
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.getByText("Kubernetes: unknown")).toBeTruthy();
    expect(screen.getByText("Freshness: fresh")).toBeTruthy();
  });
  it("does not infer readiness zero from missing evidence", () => {
    render(<WorkloadHealth summary={null} />);
    expect(screen.getByText(/Readiness: Unknown/)).toBeTruthy();
    expect(screen.getByText("No complete live observation")).toBeTruthy();
  });
});
