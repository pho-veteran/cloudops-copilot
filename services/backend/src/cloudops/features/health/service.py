"""Passive assembly of runtime, configuration and retained feature evidence."""

import os
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.orm import Session

from cloudops.features.health.schemas import (
    CollectionDispatch,
    HealthObservation,
    HealthTarget,
    HealthView,
)
from cloudops.features.investigations.collector import configured_source_modes
from cloudops.features.investigations.service import collection_dispatch_status
from cloudops.features.recommendations.schemas import ReportResult
from cloudops.features.recommendations.service import latest_qualified_report, runner_configured
from cloudops.features.workloads.observation_health import scoped_evidence
from cloudops.features.workloads.observation_schemas import Observation
from cloudops.features.workloads.observation_settings import POLICY
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now

REASONS = {
    "database_connected": "Read from the application database.",
    "heartbeat_missing": "The worker has not recorded a heartbeat.",
    "heartbeat_fresh": "The worker recorded a recent heartbeat. Check dispatch and job progress separately.",
    "heartbeat_stale": "The last worker heartbeat is stale. Check the worker process.",
    "invalid_evidence_time": "The recorded time is ahead of the assessment clock.",
    "collector_unconfigured": "Configure a source adapter to collect investigation evidence.",
    "collector_configured": "Source adapters are configured. Open investigation attempts for collection results.",
    "collector_fixture": "The synthetic adapter produces development fixture evidence.",
    "collector_configuration_invalid": "Check the trusted collection source configuration.",
    "scope_not_configured": "There is no configured Kubernetes observation target.",
    "scope_target_missing": "The configured Deployment was missing at its last scope check.",
    "scope_target_unavailable": "The exact configured workload is unavailable. Inspect workload scope.",
    "fixture_target": "This workload is a development fixture.",
    "reader_unconfigured": "The trusted Kubernetes reader configuration is missing or invalid.",
    "scope_configuration_changed": "The configured scope differs from its last applied configuration.",
    "no_matching_observation": "The current workload and reader binding have no matching observation.",
    "observation_pending": "The workload observer is reading this target.",
    "observation_expired": "The observation deadline or retained evidence has expired.",
    "observation_partial": "The latest observation covered only part of the target evidence.",
    "observation_denied": "The Kubernetes reader was denied. Check its scoped permissions.",
    "observation_failed": "The latest observation is unavailable. Inspect workload observation history.",
    "observation_stale": "The last complete Kubernetes observation is stale.",
    "observation_fresh": "A recent complete Kubernetes observation matches this target and configuration.",
    "model_feature_view": "Open Model connection for deployment, transport and qualification evidence.",
    "executor_unconfigured": "No isolated executor is configured.",
    "executor_feature_view": "Open a workload to inspect recovery authority for its exact UID.",
    "runner_unconfigured": "No KRR runner endpoint is configured.",
    "runner_not_observed": "A KRR endpoint is configured. Current runner availability is unknown.",
    "report_invalid": "The retained resource report has invalid evidence.",
    "report_not_observed": "No qualified resource report has been retained.",
    "report_retained": "A qualified report was retained. Open it for its source evidence and applicability.",
}


def observation(now, component, status, source, reason_code, **evidence):
    return HealthObservation(
        component=component,
        status=status,
        source=source,
        assessed_at=now,
        reason_code=reason_code,
        reason=REASONS[reason_code],
        **evidence,
    )


def worker_observation(db, now):
    beat = db.get(WorkerHeartbeat, "collector")
    threshold = settings().worker_stale_seconds
    observed = beat.observed_at if beat else None
    if observed is None:
        status, reason = "never_observed", "heartbeat_missing"
    elif observed > now:
        status, reason = "unknown", "invalid_evidence_time"
        observed = None
    elif now - observed > timedelta(seconds=threshold):
        status, reason = "stale", "heartbeat_stale"
    else:
        status, reason = "fresh", "heartbeat_fresh"
    return observation(
        now,
        "worker",
        status,
        "runtime",
        reason,
        observed_at=observed,
        last_success_at=observed,
        stale_after_seconds=threshold,
    )


def collector_observation(now):
    try:
        modes = configured_source_modes(os.environ.get("COLLECTION_SOURCE_MODE", "none"))
    except ValueError:
        return observation(
            now,
            "collector",
            "unavailable",
            "collection_configuration",
            "collector_configuration_invalid",
        )
    if "development_fixture" in modes:
        status, source, reason = "configured", "development_fixture", "collector_fixture"
    elif modes:
        status, source, reason = "configured", "collection_configuration", "collector_configured"
    else:
        status, source, reason = (
            "unconfigured",
            "collection_configuration",
            "collector_unconfigured",
        )
    return observation(now, "collector", status, source, reason, configured_adapters=modes)


def evidence_time(value: datetime | None, cutoff: datetime) -> datetime | None:
    return value if value and value.tzinfo is not None and value <= cutoff else None


def cluster_observation(db, now):
    evidence = scoped_evidence(db, now)
    workload, target, state, assessment = (
        evidence.workload,
        evidence.target,
        evidence.state,
        evidence.assessment,
    )
    scope = target or workload
    public_target = (
        HealthTarget(
            workload_id=workload.id if workload else None,
            uid=workload.uid if workload else None,
            cluster=scope.cluster,
            namespace=scope.namespace,
            name=scope.name,
        )
        if scope
        else None
    )
    cutoff = now + timedelta(seconds=1)
    last_success = evidence_time(state.last_success_at, cutoff) if state else None
    observed = None
    source = "live_kubernetes"
    if target and target.state == "missing":
        status, reason = "unavailable", "scope_target_missing"
    elif target and (target.state != "current" or not workload or not workload.active):
        status, reason = "unavailable", "scope_target_unavailable"
    elif not workload:
        status, reason = "unconfigured", "scope_not_configured"
    elif workload.source == "development_fixture":
        status, reason, source = "unavailable", "fixture_target", "development_fixture"
    elif not evidence.reader_configured:
        status, reason = "unconfigured", "reader_unconfigured"
    elif not evidence.config_matches_scope:
        status, reason = "unknown", "scope_configuration_changed"
    elif not state or not state.last_attempt_at:
        status, reason = "never_observed", "no_matching_observation"
    elif not assessment:
        status, reason = "unknown", "no_matching_observation"
    elif state.latest_outcome == "partial":
        if state.last_failure_at and state.last_failure_at < now - timedelta(
            days=POLICY.history_days
        ):
            status, reason = "unavailable", "observation_expired"
        elif state.partial_payload:
            observed = evidence_time(
                Observation.model_validate(state.partial_payload).observed_at, cutoff
            )
            status, reason = (
                ("partial", "observation_partial")
                if observed
                else ("unknown", "invalid_evidence_time")
            )
        else:
            status, reason = "unknown", "no_matching_observation"
    elif assessment.coverage == "complete":
        status, reason, observed = "fresh", "observation_fresh", last_success
    elif "lease_expired" in assessment.reasons or "history_expired" in assessment.reasons:
        status, reason = "unavailable", "observation_expired"
    elif (
        evidence.failure_matches
        and state.last_failure_reason == "permission_denied"
        and (state.latest_outcome != "running" or state.success_sequence != state.sequence - 1)
        and "permission_denied" in assessment.reasons
    ):
        status, reason = "denied", "observation_denied"
    elif "stale" in assessment.reasons and evidence.success_matches:
        status, reason, observed = "stale", "observation_stale", last_success
    elif state.last_success_at and last_success is None:
        status, reason = "unknown", "invalid_evidence_time"
    elif state.latest_outcome == "running" and (
        not evidence.failure_matches or "not_observed" in assessment.reasons
    ):
        status, reason = "unknown", "observation_pending"
    elif state.latest_outcome == "complete":
        status, reason = "unknown", "no_matching_observation"
    else:
        status, reason = "unavailable", "observation_failed"

    return observation(
        now,
        "kubernetes",
        status,
        source,
        reason,
        target=public_target,
        config_id=evidence.config_id,
        observed_at=observed,
        last_attempt_at=evidence_time(state.last_attempt_at, cutoff) if state else None,
        last_success_at=last_success,
        last_success_config_id=evidence.last_success_config_id if last_success else None,
        last_failure_at=evidence_time(state.last_failure_at, cutoff) if state else None,
        stale_after_seconds=POLICY.stale_seconds,
    )


def report_observation(db, now):
    row = latest_qualified_report(db)
    if not row:
        return observation(now, "krr_report", "never_observed", "live_krr", "report_not_observed")
    finished = evidence_time(row.finished_at, now)
    if finished is None:
        return observation(now, "krr_report", "unknown", "live_krr", "invalid_evidence_time")
    try:
        result = ReportResult.model_validate(row.result_v2)
    except ValidationError:
        result = None
    if not result or result.state != "available" or result.source != "live_krr":
        return observation(now, "krr_report", "unknown", "live_krr", "report_invalid")
    source = result.source_evidence
    invocation = result.tool_invocation
    identity = result.identity
    return observation(
        now,
        "krr_report",
        "historical",
        "live_krr",
        "report_retained",
        report_id=row.id,
        last_success_at=finished,
        config_id=source.config_digest if source else None,
        observed_at=evidence_time(source.collected_at, finished) if source else None,
        last_attempt_at=evidence_time(invocation.started_at, finished) if invocation else None,
        target=HealthTarget(
            workload_id=identity.workload_id,
            uid=identity.uid,
            cluster=identity.cluster,
            namespace=identity.namespace,
            name=identity.name,
        )
        if identity
        else None,
    )


def project(db: Session) -> HealthView:
    now = database_now(db)
    allowed, dispatch_reason = collection_dispatch_status()
    executor_configured = bool(os.environ.get("EXECUTOR_URL"))
    krr_configured = runner_configured()
    return HealthView(
        collection_dispatch=CollectionDispatch(
            status="unknown" if allowed is None else "allowed" if allowed else "paused",
            reason_code=dispatch_reason,
        ),
        observations=[
            observation(
                now,
                "database",
                "available",
                "runtime",
                "database_connected",
                observed_at=now,
                last_success_at=now,
            ),
            worker_observation(db, now),
            collector_observation(now),
            cluster_observation(db, now),
            observation(now, "diagnosis", "see_feature", "feature_view", "model_feature_view"),
            observation(
                now,
                "executor",
                "see_feature" if executor_configured else "unconfigured",
                "feature_view",
                "executor_feature_view" if executor_configured else "executor_unconfigured",
            ),
            observation(
                now,
                "krr_runner",
                "unknown" if krr_configured else "unconfigured",
                "live_krr",
                "runner_not_observed" if krr_configured else "runner_unconfigured",
            ),
            report_observation(db, now),
        ],
    )
