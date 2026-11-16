// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { components } from "@/lib/api/generated/schema";
import { ExecutionOutcome, VerificationDetails } from "./RecoveryEvidence";

afterEach(cleanup);
type Outcome = components["schemas"]["Outcome"];

it.each([
  ["outcome_unknown", "outcome_unknown", "inconclusive", /Matching current fields do not establish a write/],
  ["manual_intervention", "acknowledged", "inconclusive", /write was acknowledged; application recovery is inconclusive/],
  ["failed", "acknowledged", "failed", /failed/],
  ["no_op", "not_sent", "not_performed", /excluded from remediation success/],
  ["rolled_back", "acknowledged", "passed", /original forward result remains unchanged/],
] as const)("shows %s with independent attribution and verification", (state, attribution, verification, explanation) => {
  const outcome: Outcome = { schema_version: "RecoveryOutcome/v1", operation_id: "00000000-0000-4000-8000-000000000001", attribution, verification, future_authority: "fresh_preflight_required", reason: "Controlled outcome fixture; no live write." };
  render(<ExecutionOutcome state={state} outcome={outcome} />);
  expect(screen.getAllByText(explanation).length).toBeGreaterThan(0);
  expect(screen.getByText("Write attribution")).toBeTruthy();
  expect(screen.getByText("Future write authority")).toBeTruthy();
});

it("does not present reserved verification slots as measured coverage", () => {
  render(<VerificationDetails value={{ execution_id: "00000000-0000-4000-8000-000000000001", profile: {}, profile_digest: "controlled", baseline: null, acknowledged_at: null, deadline: null, next_slot: 3, samples: {}, result: {}, finalized_at: null }} />);
  expect(screen.getByText("3; stored observations 0")).toBeTruthy();
  expect(screen.getByText("No acknowledged window")).toBeTruthy();
  expect(screen.getAllByText(/Not recorded/).length).toBeGreaterThan(0);
  expect(screen.getByText(/Reserved slots are not passing samples/)).toBeTruthy();
});
