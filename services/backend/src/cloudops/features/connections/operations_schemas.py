from datetime import datetime
from typing import Literal
from uuid import UUID

from cloudops.shared.dispatch_permit import DenyReason
from cloudops.shared.schemas import View

JobKind = Literal[
    "development_collection_v1",
    "diagnosis_analysis_v1",
    "diagnosis_probe_v1",
    "recommendation_report_v1",
    "recovery_execution_v1",
    "recovery_reconciliation_v1",
    "recovery_verification_v1",
]


class VerificationSlots(View):
    due: int
    future: int
    earliest_future_at: datetime | None


class JobCounts(View):
    queued: int
    running: int
    held: int
    runnable: int
    due_retry: int
    future_retry: int
    expired_lease: int | None
    earliest_future_retry_at: datetime | None
    verification_slots: VerificationSlots | None


class JobKindStatus(View):
    kind: JobKind
    counts: JobCounts | None
    dispatch: Literal["allowed", "paused"]
    dispatch_reason: DenyReason | None


class BlockedRun(View):
    incident_id: UUID
    incident_title: str
    run_id: UUID
    created_at: datetime


class QuotaWaitingRun(BlockedRun):
    earliest_retry_at: datetime | None


class OperationsView(View):
    assessed_at: datetime
    jobs: list[JobKindStatus]
    jobs_capped: bool
    job_read_limit: int
    oldest_runnable_age_seconds: int | None
    capacity_deferred: list[BlockedRun]
    capacity_deferred_has_more: bool
    quota_waiting: list[QuotaWaitingRun]
    quota_waiting_has_more: bool
    run_sample_limit: int
