import type { components } from "@/lib/api/generated/schema";

export const recoveryAvailabilityFixture = (workloadId: string, uid = "controlled-deployment-uid"): components["schemas"]["RecoveryAvailability"] => ({
  workload_id: workloadId, uid, observed_at: "2026-11-16T12:00:00Z",
  target_authority: "enabled", application_state: "ready_for_preflight", epoch: "controlled-epoch",
  reason: "Controlled gateway observation; no live write or recovery claim.",
  qualified_templates: ["scale_deployment"], blocking_executions: [], blocking_executions_truncated: false,
  recent_executions: [],
});

export const reviewClockFixture = (serverTime = new Date().toISOString()) => ({
  server_time: serverTime,
  review_expires_at: new Date(Date.parse(serverTime) + 60000).toISOString(),
});
