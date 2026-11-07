// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { buildIncidentHash, readRoute, safeRoute } from "@/app/routes";
import { HandoffReferences, storedReferences } from "./HandoffReferences";

const incident = "00000000-0000-4000-8000-000000000010";
const left = "00000000-0000-4000-8000-000000000020";
const right = "00000000-0000-4000-8000-000000000030";
const evidence = "00000000-0000-4000-8000-000000000040";
const key = `v1_${"a".repeat(64)}`;
afterEach(cleanup);

it("preserves both comparison identities, filter and locator through canonical reload", () => {
  const selection = {
    leftIncident: incident,
    leftRun: left,
    rightIncident: incident,
    rightRun: right,
    changedOnly: true,
    differenceStatus: "changed",
    source: "kubernetes.state",
    catalog: "standard_v1",
    locatorKind: "field",
    locatorKey: key,
  };
  const hash = buildIncidentHash(
    incident,
    right,
    "#/queue?state=open",
    "",
    evidence,
    selection,
  );
  window.history.replaceState(null, "", `/${hash}`);
  expect(readRoute().selection).toEqual(selection);
  expect(window.location.hash).toBe(hash);
});

it("drops citation-only locators when navigating back to comparison sources", () => {
  const hash = buildIncidentHash(incident, left, "#/queue", "", "", {
    leftIncident: incident,
    leftRun: left,
    rightIncident: incident,
    rightRun: right,
    source: "kubernetes.events",
    catalog: "standard_v1",
    changedOnly: true,
    locatorKind: "field",
    locatorKey: key,
  });
  const route = safeRoute(hash);
  expect(route.page).toBe("incidents");
  expect(route.selection).toMatchObject({
    rightRun: right,
    source: "kubernetes.events",
    changedOnly: true,
  });
  expect(route.selection?.locatorKey).toBeUndefined();
  expect(route.selection?.locatorKind).toBeUndefined();
});

it.each([
  `compare_run=${right}`,
  `compare_run=${right}&compare_incident=${incident}&compare_run=${left}`,
  `compare_run=${right}&compare_incident=foreign`,
  "locator_kind=field",
  `locator_kind=field&locator_key=../../arbitrary`,
  `compare_source=kubernetes.state`,
  `compare_incident=${incident}&compare_run=${right}&compare_filter=unsafe`,
  `compare_incident=${incident}&compare_run=${right}&compare_status=unsafe`,
])("rejects unsafe or incomplete selection %s", (params) => {
  expect(
    safeRoute(
      `#/incidents/${incident}?run=${left}&evidence=${evidence}&${params}`,
    ).page,
  ).toBe("queue");
});

it("takes saved references only from structured snapshot IDs, never Markdown", () => {
  const snapshot = {
    evidence: { items: [{ id: evidence, run_id: left }] },
    citations: {
      items: [
        {
          run_id: left,
          evidence_id: evidence,
          locator: { kind: "field", key },
        },
        {
          run_id: right,
          evidence_id: evidence,
          locator: { kind: "field", key },
        },
      ],
    },
    markdown: `Latest Run ${right}`,
  };
  expect(storedReferences(snapshot)).toEqual([
    { run: left, evidence },
    { run: left, evidence, kind: "field", key },
  ]);
  render(
    <HandoffReferences
      incidentId={incident}
      resolutionId={right}
      snapshot={snapshot}
    />,
  );
  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(2);
  expect(links[1].getAttribute("href")).toContain(`run=${left}`);
  expect(links[1].getAttribute("href")).toContain(`resolution=${right}`);
  expect(links[1].getAttribute("href")).toContain(`locator_key=${key}`);
});

it("does not backfill a legacy handoff from current evidence", () => {
  render(
    <HandoffReferences
      incidentId={incident}
      resolutionId={right}
      snapshot={{ text: `run=${left}&evidence=${evidence}` }}
    />,
  );
  expect(screen.getByText(/references not captured/)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});
