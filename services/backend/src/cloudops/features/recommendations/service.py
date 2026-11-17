"""Feature-owned admission snapshots, frozen exports and read-only projections."""

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.schemas import (
    Applicability,
    AttemptView,
    RecommendationObservation,
    ReportIdentity,
    ReportProfile,
    ReportResult,
    ReportView,
)
from cloudops.features.workloads.models import ScopeState, Workload
from cloudops.features.workloads.observation_models import ObservationState, WorkloadObservation
from cloudops.features.workloads.observation_schemas import Observation
from cloudops.features.workloads.observation_service import summary as observation_summary
from cloudops.shared.database import WorkerHeartbeat, database_now
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt

REPORT_JOB = "recommendation_report_v1"
_dispatch_status = None
PUBLIC_REASONS = {
    "producer_contract_unavailable": "Resource advice is unavailable until trusted usage history and KRR results are qualified.",
    "representative_history_unqualified": "KRR ran, but representative usage history is not qualified; no numeric advice is available.",
    "runner_failed": "The KRR invocation failed. Inspect its retained tool receipt before requesting another report.",
    "worker_failed": "The bounded report job failed. Its attempts are retained; an Operator can request a new report.",
}
LEGACY_FIELDS = (
    "id",
    "workload_id",
    "state",
    "requested_hours",
    "tool_version",
    "result",
    "reason",
    "created_by",
    "created_at",
    "finished_at",
)


def configure_dispatch_status(reader):
    global _dispatch_status
    _dispatch_status = reader


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def capture_identity(db, workload):
    scope = db.get(ScopeState, 1)
    row = db.scalar(
        select(WorkloadObservation)
        .where(
            WorkloadObservation.workload_id == workload.id,
            WorkloadObservation.epoch == workload.observation_epoch,
        )
        .order_by(WorkloadObservation.observed_at.desc(), WorkloadObservation.id.desc())
        .limit(1)
    )
    identity = ReportIdentity(
        workload_id=workload.id,
        cluster=workload.cluster,
        namespace=workload.namespace,
        name=workload.name,
        uid=workload.uid,
        cluster_fingerprint=scope.fingerprint
        if scope and scope.cluster_key == workload.cluster
        else None,
    )
    if row:
        try:
            observation = Observation.model_validate(row.payload)
            if (
                observation.identity.workload_id != workload.id
                or observation.identity.uid != workload.uid
                or observation.identity.epoch != workload.observation_epoch
            ):
                return identity
            fields = [x.model_dump(mode="json") for x in observation.template_fields]
            identity.observation_id, identity.observed_at = row.id, observation.observed_at
            identity.template_digest = digest(fields)
            identity.pod_template_digest = observation.pod_template_digest
            identity.resources_digest = digest(
                [{k: f[k] for k in ("name", "requests", "limits")} for f in fields]
            )
            identity.containers = [f["name"] for f in fields]
        except (ValidationError, KeyError, TypeError):
            pass
    return identity


def prepare_report(db, workload, actor_id, hours=6):
    identity = capture_identity(db, workload)
    return RecommendationReport(
        workload_id=workload.id,
        created_by=actor_id,
        requested_hours=hours,
        schema_version=2,
        input_identity=identity.model_dump(mode="json"),
        profile=ReportProfile(history_hours=hours).model_dump(mode="json"),
    )


def legacy_bytes(row):
    values = {}
    for key in LEGACY_FIELDS:
        value = getattr(row, key)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat().replace("+00:00", "Z")
        values[key] = value
    return json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def freeze_legacy(row):
    if row.schema_version != 1 or row.export_text is not None:
        return
    row.export_text = legacy_bytes(row)
    row.export_sha256 = hashlib.sha256(row.export_text.encode()).hexdigest()


def applicability(db, row, now):
    if not row.result_v2:
        return Applicability(
            state="unknown",
            assessed_at=now,
            reasons=["legacy_report" if row.schema_version == 1 else "report_not_final"],
        )
    result = ReportResult.model_validate(row.result_v2)
    reasons = []
    if result.window:
        end = (
            result.window.end
            if result.window.kind == "exact"
            else result.window.start + timedelta(hours=row.requested_hours)
        )
        if now >= end + timedelta(hours=24):
            reasons.append("report_aged")
    workload = db.get(Workload, row.workload_id)
    if not workload or not workload.active:
        reasons.append("workload_inactive")
    if reasons:
        return Applicability(state="stale", assessed_at=now, reasons=reasons)
    current = capture_identity(db, workload)
    expected = result.identity
    for key in (
        "cluster",
        "namespace",
        "name",
        "uid",
        "cluster_fingerprint",
        "template_digest",
        "pod_template_digest",
        "resources_digest",
    ):
        old, new = getattr(expected, key), getattr(current, key)
        if old is not None and new is not None and old != new:
            reasons.append("current_identity_changed")
            break
    if reasons:
        return Applicability(state="stale", assessed_at=now, reasons=reasons)
    if result.window is None:
        return Applicability(
            state="unknown", assessed_at=now, reasons=["source_window_unavailable"]
        )
    known = current.observed_at and now - timedelta(seconds=60) <= current.observed_at <= now
    state = db.get(ObservationState, workload.id)
    retained = (
        db.get(WorkloadObservation, current.observation_id) if current.observation_id else None
    )
    observation = observation_summary(workload, state, retained, now)
    known = known and observation.coverage == "complete" and observation.freshness == "fresh"
    if not known or any(
        getattr(expected, k) is None or getattr(current, k) is None
        for k in (
            "cluster_fingerprint",
            "template_digest",
            "pod_template_digest",
            "resources_digest",
        )
    ):
        return Applicability(
            state="unknown", assessed_at=now, reasons=["current_observation_unavailable"]
        )
    return Applicability(state="current", assessed_at=now, reasons=[])


def report_view(db, row):
    now = database_now(db)
    job = db.scalar(select(Job).where(Job.kind == REPORT_JOB, Job.subject_id == row.id))
    attempts = (
        []
        if not job
        else db.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id)
            .order_by(JobAttempt.number)
            .limit(3)
        ).all()
    )
    phase = (
        "failed"
        if row.migration_disposition == "interrupted_at_cutover"
        or (job and job.state in {"failed", "cancelled"})
        else row.state
        if row.state in {"queued", "running"}
        else "finished"
    )
    allowed, pause_reason = _dispatch_status() if _dispatch_status else (False, "permit_missing")
    return ReportView(
        **{k: getattr(row, k) for k in LEGACY_FIELDS},
        schema_version=row.schema_version,
        input_identity=row.input_identity,
        profile=row.profile,
        result_v2=row.result_v2,
        migration_disposition=row.migration_disposition,
        job_phase=phase,
        result_state=row.result_v2["state"] if row.result_v2 else None,
        attempts=[AttemptView.model_validate(a) for a in attempts],
        dispatch_paused=phase in {"queued", "running"} and not allowed,
        dispatch_reason=pause_reason if phase in {"queued", "running"} and not allowed else None,
        applicability=applicability(db, row, now),
        export_ready=row.export_text is not None,
        export_sha256=row.export_sha256,
    )


def publish(db, row, value):
    if row.schema_version != 2 or row.export_text is not None:
        raise ValueError("Only an unfinished versioned report can publish.")
    result = ReportResult.model_validate(value)
    original = ReportIdentity.model_validate(row.input_identity)
    for key in ("workload_id", "cluster", "namespace", "name", "uid", "cluster_fingerprint"):
        if getattr(original, key) is not None and getattr(original, key) != getattr(
            result.identity, key
        ):
            raise ValueError("Report output does not match its admitted workload identity.")
    expected_profile = ReportProfile.model_validate(row.profile).model_dump()
    actual_profile = result.profile.model_dump()
    if any(
        value is not None and actual_profile[key] != value
        for key, value in expected_profile.items()
    ):
        raise ValueError("Report output does not match its admitted tool/profile.")
    data = result.model_dump(mode="json")
    if len(canonical(data).encode()) > 262144:
        raise ValueError("The bounded report payload exceeds 256 KiB.")
    row.result_v2, row.state = data, result.state
    row.reason = PUBLIC_REASONS.get(
        result.reason,
        f"Resource report: {result.state.replace('_', ' ')}. Inspect each resource's coverage and reasons.",
    )
    row.tool_version = "krr-v" + result.profile.tool_version
    row.result = {
        "source": result.source,
        "coverage": None,
        "recommendations": None,
        "tool_exit_code": result.tool_exit_code,
        "output_sha256": result.output_sha256,
    }
    row.finished_at = database_now(db)
    db.flush()
    view = report_view(db, row).model_dump(mode="json")
    for key in (
        "applicability",
        "dispatch_paused",
        "dispatch_reason",
        "export_ready",
        "export_sha256",
    ):
        view.pop(key)
    # Shared finish commits the current attempt's success immediately after this callback.
    for attempt in view["attempts"]:
        if attempt["state"] == "running" and attempt["finished_at"] is not None:
            attempt["state"] = "succeeded"
    row.export_text = canonical(view)
    row.export_sha256 = hashlib.sha256(row.export_text.encode()).hexdigest()


def unavailable(row, reason="producer_contract_unavailable"):
    return ReportResult(
        state="unavailable",
        reason=reason,
        source="unconfigured",
        identity=ReportIdentity.model_validate(row.input_identity),
        profile=ReportProfile.model_validate(row.profile),
        limitations=["No qualified structured KRR producer has published advice."],
    )


def feature_observation(db):
    now = database_now(db)
    success = db.scalar(
        select(RecommendationReport)
        .where(RecommendationReport.schema_version == 2, RecommendationReport.state == "available")
        .order_by(RecommendationReport.finished_at.desc())
        .limit(1)
    )
    latest = db.scalar(
        select(RecommendationReport)
        .order_by(RecommendationReport.created_at.desc(), RecommendationReport.id.desc())
        .limit(1)
    )
    beat = db.get(WorkerHeartbeat, "collector")
    configured = bool(os.environ.get("KRR_RUNNER_URL"))
    status, reason = "unavailable", "runner_unconfigured"
    if configured:
        reason = "no_qualified_report"
        if success:
            stale = (
                not beat
                or now - beat.observed_at >= timedelta(seconds=45)
                or now - success.finished_at >= timedelta(hours=24)
            )
            status, reason = (
                ("stale", "observation_stale") if stale else ("available", "last_report_succeeded")
            )
            if latest and latest.state == "unavailable" and latest.created_at >= success.created_at:
                status, reason = "unavailable", "latest_report_unavailable"
    return RecommendationObservation(
        status=status,
        reason=reason,
        observed_at=now,
        last_success_at=success.finished_at if success else None,
        source_kind="live_krr" if configured else "unconfigured",
    )


def encode_cursor(row, workload_id):
    return base64.urlsafe_b64encode(
        canonical(
            {
                "at": row.created_at.isoformat(),
                "id": str(row.id),
                "workload": str(workload_id) if workload_id else None,
            }
        ).encode()
    ).decode()


def decode_cursor(value, workload_id):
    try:
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if set(data) != {"at", "id", "workload"} or data["workload"] != (
            str(workload_id) if workload_id else None
        ):
            raise ValueError
        at, identifier = datetime.fromisoformat(data["at"]), UUID(data["id"])
        if at.tzinfo is None:
            raise ValueError
        return at, identifier
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            422, "cursor_invalid", "Use the cursor with its original workload filter."
        ) from None
