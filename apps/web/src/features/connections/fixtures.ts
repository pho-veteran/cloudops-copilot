import type { components } from "@/lib/api/generated/schema";

type Snapshot = components["schemas"]["OperationsView"];
type Job = components["schemas"]["JobKindStatus"];
type Receipt = components["schemas"]["AlertReceiptView"];
type ReceiptPage = components["schemas"]["AlertReceiptPage"];

export const capacityIncidentId = "00000000-0000-4000-8000-000000000010";
export const capacityRunId = "00000000-0000-4000-8000-000000000020";
export const quotaIncidentId = "00000000-0000-4000-8000-000000000011";
export const quotaRunId = "00000000-0000-4000-8000-000000000021";
const assessedAt = "2026-11-18T10:00:00Z";
const retryAt = "2026-11-18T10:03:00Z";
const otherJobKinds: Job["kind"][] = [
  "diagnosis_analysis_v1",
  "diagnosis_probe_v1",
  "recommendation_report_v1",
  "recovery_execution_v1",
  "recovery_reconciliation_v1",
  "recovery_verification_v1",
];

export function operationsFixture(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    assessed_at: assessedAt,
    jobs: [
      {
        kind: "development_collection_v1",
        dispatch: "paused",
        dispatch_reason: "recovery_mode",
        counts: {
          queued: 3,
          running: 2,
          held: 0,
          runnable: 2,
          due_retry: 1,
          future_retry: 1,
          expired_lease: 1,
          earliest_future_retry_at: retryAt,
          verification_slots: null,
        },
      },
      ...otherJobKinds.map((kind): Job => ({
        kind,
        dispatch: "allowed",
        dispatch_reason: null,
        counts: {
          queued: 0,
          running: 0,
          held: 0,
          runnable: 0,
          due_retry: 0,
          future_retry: 0,
          expired_lease: 0,
          earliest_future_retry_at: null,
          verification_slots: kind === "recovery_verification_v1"
            ? { due: 0, future: 0, earliest_future_at: null }
            : null,
        },
      })),
    ],
    jobs_capped: false,
    job_read_limit: 1000,
    oldest_runnable_age_seconds: 80,
    capacity_deferred: [
      {
        incident_id: capacityIncidentId,
        incident_title: "Checkout latency",
        run_id: capacityRunId,
        created_at: assessedAt,
      },
    ],
    capacity_deferred_has_more: false,
    quota_waiting: [
      {
        incident_id: quotaIncidentId,
        incident_title: "Ledger startup",
        run_id: quotaRunId,
        created_at: assessedAt,
        earliest_retry_at: retryAt,
      },
    ],
    quota_waiting_has_more: false,
    run_sample_limit: 20,
    ...overrides,
  };
}

export function alertReceiptFixture(overrides: Partial<Receipt> = {}): Receipt {
  return {
    id: crypto.randomUUID(),
    delivery_id: "00000000-0000-4000-8000-000000000001",
    received_at: assessedAt,
    source_id: "lab-alertmanager",
    alert_status: "firing",
    fingerprint: "a1",
    alertname: "CheckoutDown",
    namespace: "demo",
    target_kind: "deployment",
    target_name: "checkout",
    outcome: "deferred",
    reason: null,
    incident_id: capacityIncidentId,
    run_id: capacityRunId,
    workload_id: "00000000-0000-4000-8000-000000000030",
    ...overrides,
  };
}

export function alertReceiptsPageFixture(overrides: Partial<ReceiptPage> = {}): ReceiptPage {
  return {
    configured: true,
    source_id: "lab-alertmanager",
    items: [
      alertReceiptFixture({
        alertname: "LedgerUnavailable",
        target_name: "ledger",
        outcome: "rejected",
        reason: "out_of_scope",
        incident_id: null,
        run_id: null,
        workload_id: null,
      }),
      alertReceiptFixture(),
    ],
    next_cursor: null,
    ...overrides,
  };
}
