import type { components } from "@/lib/api/generated/schema";

export const workloadFixture = {
  items: [
    {
      id: "00000000-0000-4000-8000-000000000001",
      cluster: "development",
      namespace: "demo",
      name: "checkout",
      uid: "development-checkout-v1",
      active: true,
      source: "development_fixture",
      health: "unavailable",
    },
  ],
  next_offset: null,
} satisfies components["schemas"]["WorkloadPage"];

export const observationFixture = {
  workload_id: workloadFixture.items[0].id,
  workload_active: true,
  source: "development_fixture",
  availability: "unavailable",
  freshness: "unavailable",
  observation: null,
  observed_at: null,
  last_attempt_at: null,
  last_attempt_state: null,
  last_attempt_reason: null,
  pending_refresh: false,
} satisfies components["schemas"]["WorkloadObservationView"];

export const observationHistoryFixture = {
  items: [],
  next_offset: null,
} satisfies components["schemas"]["WorkloadObservationPage"];
