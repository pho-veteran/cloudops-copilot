import { describe, expect, it } from "vitest";
import {
  createInitialState,
  createScenario,
  getIncident,
  handoffMarkdown,
  reducer,
  type DemoAction,
  type DemoState,
} from "./model";

function apply(state: DemoState, ...actions: DemoAction[]) {
  return actions.reduce(reducer, state);
}
function execute(state: DemoState) {
  return apply(
    state,
    { type: "approve" },
    { type: "advance" },
    { type: "advance" },
    { type: "advance" },
  );
}

describe("CloudOps deterministic demo workflow", () => {
  it("enforces viewer/session guards and decision reasons", () => {
    const viewer = createScenario("viewer");
    expect(reducer(viewer, { type: "approve" }).error).toMatch(/Viewer/);
    expect(getIncident(reducer(viewer, { type: "approve" })).plan?.status).toBe(
      "awaiting_approval",
    );
    expect(
      reducer(createScenario("session-expired"), {
        type: "add_note",
        reason: "note",
      }).error,
    ).toMatch(/Session expired/);
    expect(
      reducer(createInitialState(), { type: "reject", reason: " " }).error,
    ).toMatch(/reason/);
  });

  it("does not count analysis completion as recovery and keeps incidents independent", () => {
    const original = createInitialState();
    const queued = reducer(original, {
      type: "approve",
      reason: "Reviewed exact diff",
    });
    expect(getIncident(queued).execution.status).toBe("queued");
    expect(getIncident(queued).status).toBe("open");
    expect(getIncident(original).plan?.status).toBe("awaiting_approval");
    const other = getIncident(queued, "INC-041");
    expect(other.execution.status).toBe("none");
    expect(other.notes).toEqual([]);
    const recovered = execute(createInitialState());
    expect(getIncident(recovered).execution.status).toBe("succeeded");
    expect(getIncident(recovered).status).toBe("open");
  });

  it("blocks resolve and reinvestigation during an active execution", () => {
    const executing = apply(
      createInitialState(),
      { type: "approve" },
      { type: "advance" },
    );
    expect(
      reducer(executing, { type: "resolve", reason: "hide execution" }).error,
    ).toMatch(/Cannot resolve/);
    expect(
      reducer(executing, { type: "reinvestigate", reason: "new run" }).error,
    ).toMatch(/active/);
    expect(
      getIncident(
        reducer(executing, { type: "reinvestigate", reason: "new run" }),
      ).execution.status,
    ).toBe("executing");
  });

  it("invalidates unused plans on resolution and preserves the resolution snapshot after reopen", () => {
    const resolved = reducer(createInitialState(), {
      type: "resolve",
      reason: "Manual recovery verified elsewhere",
    });
    const snapshot = handoffMarkdown(
      getIncident(resolved),
      getIncident(resolved).resolutions[0].id,
    );
    expect(getIncident(resolved).plan?.status).toBe("invalidated");
    expect(reducer(resolved, { type: "approve" }).error).toMatch(
      /not awaiting/,
    );
    const reopened = apply(
      resolved,
      { type: "reopen", reason: "Symptoms returned" },
      { type: "add_note", reason: "New context" },
      { type: "reinvestigate", reason: "Fresh evidence" },
    );
    expect(
      handoffMarkdown(
        getIncident(reopened),
        getIncident(reopened).resolutions[0].id,
      ),
    ).toBe(snapshot);
    expect(getIncident(reopened).runs).toHaveLength(2);
  });

  it("keeps partial evidence honest and preserves its earlier run after retry", () => {
    const partial = createScenario("partial");
    expect(getIncident(partial).plan).toBeNull();
    expect(
      getIncident(partial)
        .run.evidence.filter((e) => e.status === "unavailable")
        .every((e) => !e.excerpt.includes("OOMKilled")),
    ).toBe(true);
    const old = structuredClone(getIncident(partial).runs[0]);
    const retried = apply(
      partial,
      { type: "retry" },
      { type: "advance" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(retried).runs[0]).toEqual(old);
    expect(getIncident(retried).run.status).toBe("completed");
    expect(getIncident(retried).plan?.status).toBe("awaiting_approval");
  });

  it("supports quota cancellation without pretending to cancel an in-flight call", () => {
    const quota = createScenario("quota");
    expect(getIncident(reducer(quota, { type: "cancel_run" })).run.status).toBe(
      "cancelled",
    );
    const analyzing = apply(
      quota,
      { type: "retry" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(analyzing).run.status).toBe("analyzing");
    expect(reducer(analyzing, { type: "cancel_run" }).error).toMatch(
      /in-flight/,
    );
  });

  it("requires a healthy baseline before injecting and proposing restore", () => {
    let state = createScenario("restore");
    expect(
      reducer(state, { type: "inject_failure", workloadId: "catalog" }).error,
    ).toMatch(/baseline/);
    state = apply(
      state,
      { type: "confirm_known_good", workloadId: "catalog" },
      { type: "inject_failure", workloadId: "catalog" },
      { type: "advance" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(state).plan?.template).toBe(
      "restore_deployment_template",
    );
    expect(state.healthyCandidates.catalog).toBe(false);
    expect(
      reducer(state, { type: "confirm_known_good", workloadId: "catalog" })
        .error,
    ).toMatch(/healthy/);
  });

  it("never offers execution for diagnostic-only patterns or stale/expired plans", () => {
    for (const id of ["pending", "imagepull"]) {
      const state = createScenario(id);
      expect(getIncident(state).plan).toBeNull();
      expect(reducer(state, { type: "approve" }).error).toMatch(/not awaiting/);
    }
    for (const id of ["stale", "expired"])
      expect(reducer(createScenario(id), { type: "approve" }).error).toMatch(
        /not awaiting/,
      );
  });

  it("handles failed verification and an explicitly approved rollback", () => {
    const failed = execute(createScenario("rollback"));
    expect(getIncident(failed).execution.status).toBe("failed");
    const rolledBack = apply(
      failed,
      {
        type: "request_rollback",
        reason: "Reviewed inverse diff and safe profile",
      },
      { type: "advance" },
    );
    expect(getIncident(rolledBack).execution.status).toBe("rolled_back");
    expect(getIncident(rolledBack).status).toBe("open");
    const unable = apply(
      execute(createScenario("failed-rollback")),
      { type: "request_rollback", reason: "Reviewed inverse" },
      { type: "advance" },
    );
    expect(getIncident(unable).execution.status).toBe("manual_intervention");
  });

  it("retains cross-incident locks across unknown-outcome resolution until evidence-backed verification", () => {
    let state = apply(
      createScenario("uncertain"),
      { type: "approve" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(state).execution.status).toBe("uncertain");
    expect(
      reducer(state, { type: "resolve", reason: "not yet" }).error,
    ).toMatch(/Cannot resolve/);
    state = apply(
      state,
      { type: "reconcile" },
      { type: "reconcile" },
      { type: "manual_handoff", reason: "Owner handling unavailable API" },
      { type: "resolve", reason: "Handed off with unknown retained" },
    );
    const originalId = getIncident(state).id;
    const resolution = handoffMarkdown(
      getIncident(state),
      getIncident(state).resolutions[0].id,
    );
    expect(getIncident(state).execution.outcomeUnknown).toBe(true);
    expect(state.targetLocks.checkout).toBeTruthy();
    state = apply(
      state,
      {
        type: "create_investigation",
        workloadId: "checkout",
        question: "New checkout issue",
        window: "Last 1 hour",
      },
      { type: "advance" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(reducer(state, { type: "approve" }).error).toMatch(/locked/);
    expect(
      reducer(state, {
        type: "verify_manually",
        incidentId: originalId,
        reason: "read checked",
        evidence: "fresh read",
        oldRequestStopped: false,
        outcome: "applied",
      }).error,
    ).toMatch(/old worker/);
    state = reducer(state, {
      type: "verify_manually",
      incidentId: originalId,
      reason: "API recovered; old executor stopped",
      evidence: "Fresh UID/fields and audit observation at 11:05",
      oldRequestStopped: true,
      outcome: "applied",
    });
    expect(state.targetLocks.checkout).toBeUndefined();
    expect(
      handoffMarkdown(
        getIncident(state, originalId),
        getIncident(state, originalId).resolutions[0].id,
      ),
    ).toBe(resolution);
    expect(reducer(state, { type: "approve" }).error).toBe("");
  });

  it("distinguishes no-op and inconclusive verification from success", () => {
    expect(getIncident(execute(createScenario("no-op"))).execution.status).toBe(
      "no_op",
    );
    const inconclusive = execute(createScenario("inconclusive"));
    expect(getIncident(inconclusive).execution.status).toBe(
      "manual_intervention",
    );
    expect(getIncident(inconclusive).execution.summary).toMatch(/inconclusive/);
    expect(getIncident(inconclusive).execution.outcomeUnknown).toBe(false);
  });

  it("retains manual question/window and scope-checks workload intake", () => {
    const state = reducer(createInitialState(), {
      type: "create_investigation",
      workloadId: "search",
      question: "Why did latency increase?",
      symptom: "Latency",
      window: "Last 6 hours",
    });
    expect(getIncident(state).run.question).toBe("Why did latency increase?");
    expect(getIncident(state).run.window).toBe("Last 6 hours");
    expect(getIncident(state).workloadId).toBe("search");
    expect(
      reducer(state, {
        type: "create_investigation",
        workloadId: "outside",
        question: "x",
        window: "1h",
      }).error,
    ).toMatch(/scope/);
  });

  it("keeps the prior completed finding when a later run fails", () => {
    const state = createScenario("failed-rerun");
    const incident = getIncident(state);
    expect(incident.run.status).toBe("failed");
    expect(incident.runs).toHaveLength(2);
    expect(incident.runs[0].status).toBe("completed");
    expect(incident.runs[0].summary).toContain("OOM");
    expect(incident.plan).toBeNull();
  });

  it("blocks controller/capacity and unsupported external-config recovery", () => {
    for (const id of [
      "scale-hpa",
      "scale-no-headroom",
      "restore-no-baseline",
      "restore-external-config",
    ]) {
      const state = createScenario(id);
      expect(getIncident(state).plan).toBeNull();
      expect(getIncident(state).manualReason).toBeTruthy();
      expect(reducer(state, { type: "approve" }).error).toMatch(/not awaiting/);
    }
  });

  it("guards separate rollback expiry, fresh state and old-profile safety", () => {
    const expected = {
      "rollback-expired": /expired/,
      "rollback-stale": /fields changed/,
      "rollback-unsafe": /unsafe/,
    };
    for (const [id, message] of Object.entries(expected)) {
      const state = execute(createScenario(id));
      const blocked = reducer(state, {
        type: "request_rollback",
        reason: "Review inverse",
      });
      expect(blocked.error).toMatch(message);
      expect(getIncident(blocked).execution.status).toBe("failed");
    }
  });

  it("preserves executed approval in resolution, handoff and later run history", () => {
    const succeeded = execute(createInitialState());
    const planId = getIncident(succeeded).plan!.id;
    const resolved = reducer(succeeded, {
      type: "resolve",
      reason: "Fresh post-check reviewed",
    });
    const incident = getIncident(resolved);
    expect(incident.plan?.status).toBe("approved");
    expect(incident.resolutions[0].plan?.status).toBe("approved");
    expect(handoffMarkdown(incident, incident.resolutions[0].id)).toContain(
      "approved",
    );
    const rerun = apply(
      resolved,
      { type: "reopen", reason: "New symptoms" },
      { type: "reinvestigate", reason: "New snapshot" },
    );
    expect(
      getIncident(rerun).plans.find((plan) => plan.id === planId)?.status,
    ).toBe("approved");
  });

  it("starts manual intake with only its actual queued run and no fabricated prior finding", () => {
    const state = reducer(createInitialState(), {
      type: "create_investigation",
      workloadId: "checkout",
      question: "Inspect current workload",
      window: "Last 1 hour",
    });
    const incident = getIncident(state);
    expect(incident.runs).toHaveLength(1);
    expect(incident.runs[0].status).toBe("queued");
    expect(incident.runs[0].evidence).toEqual([]);
    expect(incident.runs.some((run) => run.status === "completed")).toBe(false);
    expect(incident.plan).toBeNull();
  });

  it("does not generate OOM evidence or a recovery plan for a healthy manual workload", () => {
    const state = apply(
      createInitialState(),
      {
        type: "create_investigation",
        workloadId: "payments",
        question: "Check health",
        window: "Last 30 minutes",
      },
      { type: "advance" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(state).run.status).toBe("completed");
    expect(getIncident(state).run.summary).toMatch(/healthy/);
    expect(
      getIncident(state).run.evidence.some((evidence) =>
        evidence.excerpt.includes("OOMKilled"),
      ),
    ).toBe(false);
    expect(getIncident(state).plan).toBeNull();
  });

  it("requires reopen before rollback and permits diagnostic handoff without reconciliation", () => {
    const resolved = reducer(execute(createInitialState()), {
      type: "resolve",
      reason: "Verified",
    });
    expect(
      reducer(resolved, { type: "request_rollback", reason: "Undo" }).error,
    ).toMatch(/Reopen/);
    expect(
      getIncident(
        reducer(resolved, { type: "request_rollback", reason: "Undo" }),
      ).execution.status,
    ).toBe("succeeded");
    for (const id of ["pending", "imagepull", "partial"]) {
      const handedOff = reducer(createScenario(id), {
        type: "manual_handoff",
        reason: "Platform owner will continue these checks",
      });
      expect(handedOff.error).toBe("");
      expect(getIncident(handedOff).activity.at(-1)?.title).toBe(
        "Manual handoff recorded",
      );
    }
  });

  it("enforces the reconciliation cap in the reducer, independently of disabled UI", () => {
    const state = apply(
      createScenario("uncertain"),
      { type: "approve" },
      { type: "advance" },
      { type: "advance" },
      { type: "reconcile" },
      { type: "reconcile" },
    );
    const beyondBudget = reducer(state, { type: "reconcile" });
    expect(beyondBudget.error).toMatch(/budget exhausted/);
    expect(getIncident(beyondBudget).execution.reconcileAttempts).toBe(2);
    expect(getIncident(beyondBudget).execution.status).toBe(
      "manual_intervention",
    );
    expect(getIncident(beyondBudget).activity).toEqual(
      getIncident(state).activity,
    );
    expect(beyondBudget.targetLocks).toEqual(state.targetLocks);
  });

  it("rolls back the execution originating plan after a newer investigation without approving the new proposal", () => {
    const succeeded = execute(createInitialState());
    const oldPlan = structuredClone(getIncident(succeeded).plan!);
    const executionId = getIncident(succeeded).execution.id;
    const fresh = apply(
      succeeded,
      { type: "reinvestigate", reason: "Check fresh symptoms" },
      { type: "advance" },
      { type: "advance" },
      { type: "advance" },
    );
    expect(getIncident(fresh).plan?.id).not.toBe(oldPlan.id);
    expect(getIncident(fresh).plan?.status).toBe("awaiting_approval");
    const rollback = reducer(fresh, {
      type: "request_rollback",
      reason: "Confirm inverse of the earlier execution",
    });
    expect(rollback.error).toBe("");
    expect(getIncident(rollback).execution.id).toBe(executionId);
    expect(getIncident(rollback).execution.status).toBe("rolling_back");
    expect(getIncident(rollback).plan?.status).toBe("stale");
    expect(
      getIncident(rollback).plans.find((plan) => plan.id === oldPlan.id),
    ).toEqual(oldPlan);
    expect(getIncident(rollback).activity.at(-1)?.detail).toContain(
      oldPlan.hash,
    );
  });

  it("refuses rollback when the execution originating plan cannot be found", () => {
    const state = execute(createInitialState());
    getIncident(state).plans = [];
    const blocked = reducer(state, {
      type: "request_rollback",
      reason: "Cannot reconstruct original approval",
    });
    expect(blocked.error).toMatch(/originating plan/);
    expect(getIncident(blocked).execution.status).toBe("succeeded");
  });
});
