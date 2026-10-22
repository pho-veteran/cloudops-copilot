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
