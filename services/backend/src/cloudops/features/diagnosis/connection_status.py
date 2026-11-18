"""Read the retained model evidence without opening a provider connection."""

import os
import re
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from cloudops.features.diagnosis.api_schemas import (
    CheckView,
    ModelIdentityView,
    ModelLimitsView,
    ModelStatus,
    ProbeReceiptView,
    ProviderAttemptView,
    RCAValidationView,
    TokenUsageView,
)
from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, PROBE_JOB, dispatch_status
from cloudops.features.diagnosis.models import Analysis, AnalysisCall, AnalysisResult, ModelCheck
from cloudops.features.diagnosis.schemas import CallKind, parse_usage, safe_identifier
from cloudops.features.diagnosis.settings import load
from cloudops.shared.database import database_now

EVIDENCE_MAX_AGE = timedelta(hours=24)
CALL_KINDS = {kind.value for kind in CallKind}
FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "function_call"}


def _public_text(value, deployment):
    text = safe_identifier(value)
    credential = deployment.profile.credential if deployment.profile else None
    secret = os.environ.get(credential.name) if credential else None
    return text if text and not (secret and secret in text) else None


def _identity(raw, deployment):
    return ModelIdentityView(
        **{key: _public_text(raw.get(key), deployment) for key in ModelIdentityView.model_fields}
    )


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _number(value, low=0, high=2**53 - 1):
    return value if type(value) is int and low <= value <= high else None


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _usage(raw):
    status = raw.get("usage_status")
    if status != "observed":
        return None, "invalid" if status == "invalid" else "unavailable"
    value = raw.get("usage")
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if v is not None}
    data, parsed = parse_usage(value)
    if data is None:
        return None, parsed
    counts = {key: getattr(data, key) for key in TokenUsageView.model_fields}
    if any(count is not None and _number(count) is None for count in counts.values()):
        return None, "invalid"
    return TokenUsageView(**counts), "observed"


def check_view(check, deployment):
    raw = check.receipt
    receipt = None
    if isinstance(raw, dict):
        usage, status = _usage(raw)
        code, finish = raw.get("kind"), raw.get("finish_reason")
        schema = raw.get("schema_success")
        http_status = _number(raw.get("http_status"), 100, 599)
        transport = raw.get("transport_success")
        if http_status is not None:
            transport = 200 <= http_status < 300
        receipt = ProbeReceiptView(
            kind=code if isinstance(code, str) and code in CALL_KINDS else None,
            observed_at=_timestamp(raw.get("observed_at")),
            http_status=http_status,
            transport_success=transport if type(transport) is bool else None,
            schema_success=schema if type(schema) is bool else None,
            usage=usage,
            usage_status=status,
            requested_model=_public_text(raw.get("requested_model"), deployment),
            served_model=_public_text(raw.get("served_model"), deployment),
            response_sha256=_digest(raw.get("response_sha256")),
            response_text_sha256=_digest(raw.get("response_text_sha256")),
            finish_reason=finish if isinstance(finish, str) and finish in FINISH_REASONS else None,
            elapsed_ms=_number(raw.get("elapsed_ms")),
            response_bytes=_number(raw.get("response_bytes")),
            retry_after_seconds=_number(raw.get("retry_after_seconds")),
        )
    return CheckView(
        id=check.id,
        state=check.state,
        created_at=check.created_at,
        admitted_at=check.admitted_at,
        deadline=check.deadline,
        identity=_identity(check.identity, deployment),
        receipt=receipt,
    )


def _provider_attempt(source, admitted_at, observed_at, receipt, now, *, unknown=False):
    raw = receipt if isinstance(receipt, dict) else {}
    status = _number(raw.get("http_status"), 100, 599)
    outcome = raw.get("kind")
    usage, usage_status = _usage(raw)
    if observed_at is None or not admitted_at <= observed_at <= now:
        observed_at = None
        usage, usage_status = None, "unavailable"
        status, outcome = None, None
    return ProviderAttemptView(
        source=source,
        admitted_at=admitted_at,
        observed_at=observed_at,
        stale=now - (observed_at or admitted_at) >= EVIDENCE_MAX_AGE,
        transport=(
            "unknown" if status is None else "succeeded" if 200 <= status < 300 else "rejected"
        ),
        http_status=status,
        outcome=(
            "provider_outcome_unknown"
            if outcome == "provider_outcome_unknown" or (unknown and outcome is None)
            else outcome
            if isinstance(outcome, str) and outcome in CALL_KINDS
            else None
        ),
        usage=usage,
        usage_status=usage_status,
    )


def _retained_evidence(db, identity, now):
    checks = select(ModelCheck).where(ModelCheck.identity == identity, ModelCheck.created_at <= now)
    check = db.scalar(checks.order_by(ModelCheck.created_at.desc(), ModelCheck.id.desc()).limit(1))
    admitted_checks = checks.where(ModelCheck.admitted_at <= now).order_by(
        ModelCheck.admitted_at.desc(), ModelCheck.id.desc()
    )
    probe = db.scalar(admitted_checks.limit(1))
    calls = (
        select(AnalysisCall, Analysis.purpose)
        .join(Analysis, Analysis.id == AnalysisCall.analysis_id)
        .where(Analysis.identity == identity, AnalysisCall.admitted_at <= now)
        .order_by(AnalysisCall.admitted_at.desc(), AnalysisCall.id.desc())
    )
    call = db.execute(calls.limit(1)).first()
    latest = None
    if probe and (not call or probe.admitted_at >= call[0].admitted_at):
        raw = probe.receipt if isinstance(probe.receipt, dict) else {}
        latest = _provider_attempt(
            "probe",
            probe.admitted_at,
            _timestamp(raw.get("observed_at")),
            raw,
            now,
            unknown=probe.state == "provider_outcome_unknown",
        )
    elif call:
        attempt, purpose = call
        latest = _provider_attempt(
            "analysis" if purpose == "operational" else "evaluation",
            attempt.admitted_at,
            attempt.settled_at,
            attempt.receipt,
            now,
        )

    success_probe = db.scalar(
        admitted_checks.where(ModelCheck.receipt["http_status"].astext.regexp_match(r"^2[0-9]{2}$"))
        .with_only_columns(ModelCheck.receipt["observed_at"].astext)
        .limit(1)
    )
    success_analysis = db.scalar(
        calls.where(AnalysisCall.receipt["http_status"].astext.regexp_match(r"^2[0-9]{2}$"))
        .with_only_columns(AnalysisCall.settled_at)
        .limit(1)
    )
    last_success = max(
        (t for t in (_timestamp(success_probe), success_analysis) if t is not None and t <= now),
        default=None,
    )

    invalid = Analysis.reason == "validation_failed"
    evidence = db.execute(
        select(Analysis.purpose, Analysis.finished_at, invalid)
        .outerjoin(AnalysisResult, AnalysisResult.analysis_id == Analysis.id)
        .where(
            Analysis.identity == identity,
            Analysis.finished_at <= now,
            or_(
                invalid,
                and_(
                    Analysis.reason.in_(("validated_advice", "validated_abstention")),
                    AnalysisResult.analysis_id.is_not(None),
                ),
            ),
        )
        .order_by(Analysis.finished_at.desc(), Analysis.id.desc())
        .limit(1)
    ).first()
    rca = None
    if evidence:
        purpose, finished_at, failed = evidence
        rca = RCAValidationView(
            outcome="invalid" if failed else "validated",
            purpose=purpose,
            observed_at=finished_at,
            stale=now - finished_at >= EVIDENCE_MAX_AGE,
        )
    return check, latest, last_success, rca


def model_status(db):
    deployment = load()
    denied = deployment.denied()
    allowed, reason = dispatch_status(ANALYSIS_JOB)
    probe_allowed, probe_reason = dispatch_status(PROBE_JOB)
    identity = deployment.identity()
    now = database_now(db)
    check, latest, success, rca = (
        _retained_evidence(db, identity, now) if identity else (None, None, None, None)
    )
    profile, policy = deployment.profile, deployment.policy
    limits = None
    if profile and policy:
        output = profile.output_limit
        limits = ModelLimitsView(
            request_deadline_seconds=profile.limits.deadline_seconds,
            max_request_bytes=profile.limits.max_input_bytes,
            max_context_bytes=policy.max_target_bytes,
            output_limit_param=output.param if output else None,
            max_output_tokens=output.value if output else None,
            max_analysis_calls=3,
            max_probe_calls=1,
        )
    return ModelStatus(
        availability=denied or "available",
        identity=_identity(identity, deployment),
        quality="qualified" if denied is None else "not_established",
        qualification_cohort=(
            _public_text(deployment.qualification.cohort_id, deployment) if denied is None else None
        ),
        dispatch_allowed=allowed,
        dispatch_reason=reason,
        probe_dispatch_allowed=probe_allowed,
        probe_dispatch_reason=probe_reason,
        assessed_at=now,
        stale_after_seconds=int(EVIDENCE_MAX_AGE.total_seconds()),
        limits=limits,
        last_check=check_view(check, deployment) if check else None,
        last_attempt=latest,
        rca_validation=rca,
        last_attempt_at=latest.admitted_at if latest else None,
        last_success_at=success,
        last_error=latest.outcome if latest and latest.outcome != CallKind.TEXT else None,
    )
