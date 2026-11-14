"""One bounded model send per fenced claim, with atomic settlement/defer."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cloudops.features.diagnosis import advice, context, egress, pins
from cloudops.features.diagnosis.lifecycle import (
    ACTIVE,
    ANALYSIS_JOB,
    PROBE_JOB,
    terminal,
    dispatch_epoch,
)
from cloudops.features.diagnosis.models import (
    Analysis,
    AnalysisCall,
    AnalysisContext,
    AnalysisResult,
    ModelCheck,
)
from cloudops.features.diagnosis.settings import load
from cloudops.features.investigations.collector import Interrupted
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.service import dispatch_blocker
from cloudops.features.knowledge.guidance import pin_guidance
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, JobAttempt, _fenced, defer_owned
from cloudops.features.diagnosis.transport import ModelClient


def _run(db, analysis):
    return db.scalar(
        select(InvestigationRun)
        .where(InvestigationRun.id == analysis.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _analysis(db, subject):
    # The Job is already locked. Always Run before Analysis (also used by cancel).
    seed = db.get(Analysis, subject)
    run = _run(db, seed)
    analysis = db.scalar(
        select(Analysis)
        .where(Analysis.id == subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return analysis, run


def _unsettled(db, analysis):
    return db.scalar(
        select(AnalysisCall).where(
            AnalysisCall.analysis_id == analysis.id, AnalysisCall.settled_at.is_(None)
        )
    )


class AnalysisHandler:
    def eligible(self, db, subject):
        analysis, run = _analysis(db, subject)
        if analysis.state not in ACTIVE:
            return False
        if _unsettled(db, analysis):
            terminal(db, analysis, run, "failed", "provider_outcome_unknown")
            return False
        if analysis.dispatch_epoch != dispatch_epoch():
            terminal(db, analysis, run, "failed", "restore_quarantined")
            return False
        reason = dispatch_blocker(db, run)
        if reason or analysis.deadline <= database_now(db):
            terminal(db, analysis, run, "failed", reason or "deadline_expired")
            return False
        return True

    def started(self, db, subject):
        analysis, run = _analysis(db, subject)
        analysis.state = "analyzing"
        if analysis.purpose == "operational":
            run.state, run.provider_call = "analyzing", "none"

    def failed(self, db, subject, reason, exhausted):
        analysis, run = _analysis(db, subject)
        terminal(
            db,
            analysis,
            run,
            "failed",
            "provider_outcome_unknown" if _unsettled(db, analysis) else "attempt_failed",
        )


class ProbeHandler:
    def eligible(self, db, subject):
        check = db.get(ModelCheck, subject)
        if check.admitted_at is not None:
            check.state = "provider_outcome_unknown"
            return False
        if check.dispatch_epoch != dispatch_epoch():
            check.state = "restore_quarantined"
            return False
        if check.state != "queued" or check.deadline <= database_now(db):
            check.state = "expired" if check.state == "queued" else check.state
            return False
        return True

    def started(self, db, subject):
        pass

    def failed(self, db, subject, reason, exhausted):
        check = db.get(ModelCheck, subject)
        check.state = "provider_outcome_unknown" if check.admitted_at else "failed"


@contextmanager
def fenced(claim, gate, slot):
    # Commit on the guard's physical connection: losing it cannot publish via a new pool session.
    with gate.admitted() as admission, slot.active.begin():
        slot.verify(commit=False)
        with Session(bind=slot.active, expire_on_commit=False) as db:
            pair = _fenced(db, claim, admission)
            if pair is None:
                raise Interrupted()
            yield db, pair[0], pair[1]
            db.flush()
            slot.verify(commit=False)


def _done(db, job, attempt):
    job.state, job.error = "completed", None
    job.lease_token, job.lease_until = None, None
    attempt.state, attempt.finished_at = "succeeded", database_now(db)


def _deployment_matches(analysis):
    deployment = load()
    if deployment.denied(analysis.purpose) or deployment.identity() != analysis.identity:
        return None
    return deployment


def _prepare(claim, gate, slot):
    with fenced(claim, gate, slot) as (db, job, attempt):
        analysis, run = _analysis(db, claim.subject_id)
        deployment = _deployment_matches(analysis)
        if analysis.state not in ACTIVE or run.state == "cancelled":
            _done(db, job, attempt)
            return None
        if not deployment:
            terminal(db, analysis, run, "failed", "configuration_changed")
            _done(db, job, attempt)
            return None
        stored = db.get(AnalysisContext, analysis.id)
        if stored is None:
            selection = pin_guidance(
                db, run.incident_id, run.id, consumer="analysis", request_identity=str(analysis.id)
            )
            prepared = context.build(
                db,
                run.incident_id,
                run.id,
                selection=selection.selection,
                policy=deployment.policy,
                profile=deployment.profile,
                analysis_identity=str(analysis.id),
            )
            stored = AnalysisContext(
                analysis_id=analysis.id,
                digest=prepared.digest,
                snapshot=prepared.model_dump(mode="json"),
                summary=prepared.record(),
            )
            db.add(stored)
            if isinstance(prepared, context.InsufficientContext):
                terminal(db, analysis, run, "insufficient_evidence", prepared.reason)
                _done(db, job, attempt)
                return None
            if pins.acquire(db, analysis.id, prepared) != "acquired":
                terminal(db, analysis, run, "failed", "contributing_content_unavailable")
                _done(db, job, attempt)
                return None
        else:
            prepared = context.PreparedContext.model_validate(stored.snapshot)
        checked = egress.check_send(
            prepared,
            deployment.policy,
            deployment.profile,
            analysis.phase,
            feedback=analysis.feedback,
        )
        if not checked.ok:
            terminal(db, analysis, run, "failed", checked.code)
            _done(db, job, attempt)
            return None
        return prepared, deployment, checked


def _reserve(claim, gate, slot, checked, lease_lost, snapshot):
    if lease_lost.is_set():
        raise Interrupted()
    with fenced(claim, gate, slot) as (db, job, attempt):
        analysis, run = _analysis(db, claim.subject_id)
        if (
            analysis.state not in ACTIVE
            or run.state == "cancelled"
            or dispatch_blocker(db, run)
            or analysis.deadline <= database_now(db)
            or analysis.calls >= 3
            or _unsettled(db, analysis)
            or analysis.dispatch_epoch
            != {"authority": str(claim.permit_authority), "generation": claim.permit_generation}
            or not _deployment_matches(analysis)
        ):
            raise Interrupted()
        current = _deployment_matches(analysis)
        recheck = egress.check_send(
            snapshot, current.policy, current.profile, analysis.phase, feedback=analysis.feedback
        )
        if not recheck.ok or recheck.digest != checked.digest:
            raise Interrupted()
        analysis.calls += 1
        db.add(
            AnalysisCall(
                analysis_id=analysis.id,
                number=analysis.calls,
                lease_token=claim.token,
                phase=analysis.phase,
                request_digest=checked.digest,
                admitted_at=database_now(db),
            )
        )
        if analysis.purpose == "operational":
            run.provider_call = "in_flight"


def _receipt(call):
    # Never expose provider bodies, request ids, served model strings or raw retry headers.
    return {
        "kind": call.kind.value,
        "http_status": call.http_status,
        "usage": asdict(call.usage) if call.usage else None,
        "usage_status": call.usage_status,
        "elapsed_ms": call.elapsed_ms,
        "response_bytes": call.response_bytes,
        "retry_after_seconds": call.retry_after_seconds,
    }


def _wait(db, claim, analysis, run, mode, due, reason):
    analysis.wait_mode, analysis.earliest_retry = mode, due
    analysis.reason = reason
    analysis.state = "waiting_for_quota" if mode == "resume_required" else "pending"
    if analysis.purpose == "operational":
        run.state = "waiting_for_quota" if mode == "resume_required" else "analyzing"
        run.provider_call, run.reason = "none", "Analysis waiting: " + reason + "."
    defer_owned(db, claim, None if mode == "resume_required" else due)


def analyze(claim, gate, slot, lease_lost):
    prepared = _prepare(claim, gate, slot)
    if prepared is None:
        return
    context_snapshot, deployment, checked = prepared
    call = ModelClient(deployment.profile).send(
        list(checked.messages),
        before_send=lambda: _reserve(claim, gate, slot, checked, lease_lost, context_snapshot),
    )
    # Exact-reference validation occurs before settlement, never checkpoint raw output.
    validated = None
    if call.kind == "text":
        with sessions()() as db:
            validated = advice.validate(db, context_snapshot, call.text)
    with fenced(claim, gate, slot) as (db, job, attempt):
        analysis, run = _analysis(db, claim.subject_id)
        if not _deployment_matches(analysis):
            raise Interrupted()
        ledger = db.scalar(select(AnalysisCall).where(AnalysisCall.lease_token == claim.token))
        if ledger is None:
            # Credential/config preflight made no send and no reservation.
            terminal(db, analysis, run, "failed", call.kind.value)
            _done(db, job, attempt)
            return
        ledger.receipt, ledger.settled_at = _receipt(call), database_now(db)
        if analysis.deadline <= database_now(db):
            terminal(db, analysis, run, "failed", "deadline_expired")
        elif validated and validated.status == "valid":
            db.add(
                AnalysisResult(
                    analysis_id=analysis.id, payload=validated.result.model_dump(mode="json")
                )
            )
            terminal(db, analysis, run, "completed", "validated_advice")
        else:
            blocker = dispatch_blocker(db, run)
            if blocker:
                terminal(db, analysis, run, "failed", blocker)
                _done(db, job, attempt)
                return
            remaining = (
                analysis.calls < 3
                and job.attempts < job.max_attempts
                and analysis.phase != "repair"
            )
            feedback = (
                advice.feedback_message(deployment.profile, context_snapshot, validated.failures)
                if validated and validated.repairable and analysis.repairs < 1
                else None
            )
            if feedback and remaining:
                analysis.repairs += 1
                analysis.phase, analysis.feedback = "repair", feedback
                _wait(
                    db, claim, analysis, run, "automatic_retry", database_now(db), "repair_pending"
                )
                return
            if call.kind in {"rate_limited", "upstream_error"} and remaining:
                delay = max(30 * 2 ** (analysis.calls - 1), call.retry_after_seconds or 0)
                due = database_now(db) + timedelta(seconds=delay)
                if due < analysis.deadline:
                    if analysis.phase != "repair":
                        analysis.phase = "retry"
                    _wait(
                        db,
                        claim,
                        analysis,
                        run,
                        "resume_required" if call.kind == "rate_limited" else "automatic_retry",
                        due,
                        call.kind.value,
                    )
                    return
            reason = (
                "provider_outcome_unknown"
                if call.kind in {"timeout", "transport_error"}
                else "validation_failed"
                if validated
                else call.kind.value
            )
            terminal(db, analysis, run, "failed", reason)
        _done(db, job, attempt)


def probe(claim, gate, slot, lease_lost):
    deployment = load()
    with fenced(claim, gate, slot) as (db, job, attempt):
        check = db.get(ModelCheck, claim.subject_id)
        if (
            deployment.profile is None
            or deployment.policy is None
            or deployment.profile.mode == "disabled"
            or deployment.identity() != check.identity
        ):
            check.state = "configuration_changed"
            _done(db, job, attempt)
            return
    checked = egress.prepare_probe(deployment.policy, deployment.profile)
    if not checked.ok:
        with fenced(claim, gate, slot) as (db, job, attempt):
            db.get(ModelCheck, claim.subject_id).state = checked.code
            _done(db, job, attempt)
        return

    def reserve():
        if lease_lost.is_set():
            raise Interrupted()
        with fenced(claim, gate, slot) as (db, job, attempt):
            check = db.get(ModelCheck, claim.subject_id)
            current = load()
            if (
                check.admitted_at
                or check.deadline <= database_now(db)
                or check.dispatch_epoch
                != {"authority": str(claim.permit_authority), "generation": claim.permit_generation}
                or current.identity() != check.identity
                or current.profile is None
                or current.profile.mode == "disabled"
            ):
                raise Interrupted()
            final_check = egress.prepare_probe(current.policy, current.profile)
            if not final_check.ok or final_check.digest != checked.digest:
                raise Interrupted()
            check.admitted_at, check.state = database_now(db), "in_flight"

    call = ModelClient(deployment.profile).send(list(checked.messages), before_send=reserve)
    with fenced(claim, gate, slot) as (db, job, attempt):
        check = db.get(ModelCheck, claim.subject_id)
        if load().identity() != check.identity:
            raise Interrupted()
        check.state = (
            "provider_outcome_unknown"
            if call.kind in {"timeout", "transport_error"}
            else "completed"
        )
        check.receipt = {
            **_receipt(call),
            "transport_success": call.kind == "text",
            "schema_success": call.kind == "text" and call.text.strip() == "OK",
            "quality": "not_established",
        }
        _done(db, job, attempt)


def expire_waits():
    """No network authority required; held work must expire even while the slot is busy."""
    with sessions()() as db, db.begin():
        now = database_now(db)
        epoch = dispatch_epoch()
        jobs = db.scalars(
            select(Job)
            .outerjoin(Analysis, and_(Job.kind == ANALYSIS_JOB, Job.subject_id == Analysis.id))
            .outerjoin(ModelCheck, and_(Job.kind == PROBE_JOB, Job.subject_id == ModelCheck.id))
            .where(
                Job.kind.in_([ANALYSIS_JOB, PROBE_JOB]),
                Job.state.in_(["queued", "held", "running"]),
                or_(
                    Analysis.deadline <= now,
                    ModelCheck.deadline <= now,
                    Analysis.state.not_in(ACTIVE),
                    and_(bool(epoch), Analysis.dispatch_epoch != epoch),
                    and_(bool(epoch), ModelCheck.dispatch_epoch != epoch),
                ),
            )
            .order_by(Job.created_at)
            .with_for_update(of=Job, skip_locked=True)
            .limit(100)
        ).all()
        for job in jobs:
            if job.kind == PROBE_JOB:
                check = db.get(ModelCheck, job.subject_id)
                retired = bool(epoch) and check.dispatch_epoch != epoch
                if check.deadline > now and not retired:
                    continue
                check.state = (
                    "provider_outcome_unknown"
                    if check.admitted_at and not check.receipt
                    else "restore_quarantined"
                    if retired
                    else "expired"
                )
            else:
                analysis, run = _analysis(db, job.subject_id)
                retired = bool(epoch) and analysis.dispatch_epoch != epoch
                if analysis.deadline > now and analysis.state in ACTIVE and not retired:
                    continue
                if analysis.state in ACTIVE:
                    terminal(
                        db,
                        analysis,
                        run,
                        "failed",
                        "provider_outcome_unknown"
                        if _unsettled(db, analysis)
                        else "restore_quarantined"
                        if retired
                        else "deadline_expired",
                    )
            if job.lease_token:
                attempt = db.scalar(
                    select(JobAttempt).where(JobAttempt.lease_token == job.lease_token)
                )
                attempt.state, attempt.finished_at = "expired", now
            job.state, job.error = "failed", "Analysis expired or retired."
            job.lease_token, job.lease_until = None, None
