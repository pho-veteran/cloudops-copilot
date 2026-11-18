from datetime import timedelta

from sqlalchemy import select, text

from cloudops.features.diagnosis.api_schemas import AnalysisView
from cloudops.features.diagnosis.connection_status import check_view
from cloudops.features.diagnosis.connection_status import model_status as model_status
from cloudops.features.diagnosis.lifecycle import (
    ANALYSIS_JOB,
    PROBE_JOB,
    dispatch_epoch,
)
from cloudops.features.diagnosis.models import (
    Analysis,
    AnalysisCall,
    AnalysisContext,
    AnalysisResult,
    ModelCheck,
    ResumeReceipt,
)
from cloudops.features.diagnosis.settings import load
from cloudops.features.diagnosis.slot import lock_present
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.service import dispatch_blocker
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job, JobAttempt


def resume_denial(db, analysis, job):
    if analysis.state != "waiting_for_quota" or analysis.wait_mode != "resume_required":
        return "not_waiting_for_resume"
    if database_now(db) >= analysis.deadline:
        return "deadline_expired"
    if analysis.calls >= 3 or job.attempts >= job.max_attempts:
        return "budget_exhausted"
    if analysis.dispatch_epoch != dispatch_epoch():
        return "restore_quarantined"
    blocker = dispatch_blocker(db, db.get(InvestigationRun, analysis.run_id))
    if blocker:
        return blocker
    if analysis.earliest_retry and database_now(db) < analysis.earliest_retry:
        return "retry_not_due"
    deployment = load()
    if deployment.denied() or deployment.identity() != analysis.identity:
        return "configuration_changed"
    return None


def analysis_view(db, run_id):
    analysis = db.scalar(
        select(Analysis).where(Analysis.run_id == run_id, Analysis.purpose == "operational")
    )
    if analysis is None:
        return AnalysisView(status="not_available", reason="no_operational_analysis")
    job = db.scalar(select(Job).where(Job.kind == ANALYSIS_JOB, Job.subject_id == analysis.id))
    stored = db.get(AnalysisContext, analysis.id)
    result = db.get(AnalysisResult, analysis.id)
    calls = db.scalars(
        select(AnalysisCall)
        .where(AnalysisCall.analysis_id == analysis.id)
        .order_by(AnalysisCall.number)
    ).all()
    denial = resume_denial(db, analysis, job)
    attempts = db.scalars(
        select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.number)
    ).all()
    return AnalysisView(
        status=analysis.state,
        analysis_id=analysis.id,
        phase=analysis.phase,
        reason=analysis.reason,
        identity=analysis.identity,
        context=stored.summary if stored else None,
        result=result.payload if result else None,
        attempts=job.attempts,
        calls=analysis.calls,
        repairs=analysis.repairs,
        deadline=analysis.deadline,
        wait_mode=analysis.wait_mode,
        earliest_retry=analysis.earliest_retry,
        resume_allowed=denial is None,
        resume_denial=denial,
        model_slot_busy=analysis.state == "pending" and lock_present(db),
        attempt_history=[
            {
                "number": item.number,
                "state": item.state,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
                "error": item.error,
            }
            for item in attempts
        ],
        history=[
            {
                "number": call.number,
                "phase": call.phase,
                "admitted_at": call.admitted_at.isoformat(),
                "settled_at": call.settled_at.isoformat() if call.settled_at else None,
                "receipt": call.receipt,
            }
            for call in calls
        ],
    )


def scoped_run(db, incident_id, run_id, *, lock=False):
    query = select(InvestigationRun).where(
        InvestigationRun.id == run_id, InvestigationRun.incident_id == incident_id
    )
    run = db.scalar(query.with_for_update() if lock else query)
    if run is None:
        raise DomainError(404, "run_not_found", "Investigation not found for this incident.")
    return run


def resume(db, incident_id, run_id, actor_id, key):
    with db.begin():
        # Serialize same-key replay before taking the normal Job -> Run lock order.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 70))"), {"key": str(key)}
        )
        seed = db.scalar(
            select(Analysis).where(Analysis.run_id == run_id, Analysis.purpose == "operational")
        )
        if seed is None:
            raise DomainError(409, "analysis_unavailable", "This Run has no operational analysis.")
        job = db.scalar(
            select(Job).where(Job.kind == ANALYSIS_JOB, Job.subject_id == seed.id).with_for_update()
        )
        run = scoped_run(db, incident_id, run_id, lock=True)
        analysis = db.scalar(
            select(Analysis)
            .where(Analysis.id == seed.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        replay = db.get(ResumeReceipt, key)
        if replay:
            if replay.analysis_id != analysis.id or replay.actor_id != actor_id:
                raise DomainError(409, "idempotency_conflict", "Key belongs to another operation.")
            return analysis_view(db, run_id)
        denial = resume_denial(db, analysis, job)
        if denial:
            raise DomainError(409, denial, "This analysis cannot resume: " + denial + ".")
        if run.state == "cancelled":
            raise DomainError(409, "run_cancelled", "This investigation was cancelled.")
        analysis.state, analysis.wait_mode = "pending", None
        job.state, job.due_at = "queued", analysis.earliest_retry
        run.state, run.provider_call = "analyzing", "none"
        db.add(ResumeReceipt(idempotency_key=key, analysis_id=analysis.id, actor_id=actor_id))
        db.flush()
        return analysis_view(db, run_id)


def enqueue_check(db, actor_id, key):
    with db.begin():
        # Global bounded producer lock, independent from model-slot namespace.
        db.execute(text("SELECT pg_advisory_xact_lock(742071)"))
        prior = db.scalar(select(ModelCheck).where(ModelCheck.idempotency_key == key))
        if prior:
            if prior.actor_id != actor_id:
                raise DomainError(409, "idempotency_conflict", "Key belongs to another operator.")
            return check_view(prior, load())
        deployment = load()
        if (
            deployment.profile is None
            or deployment.policy is None
            or deployment.profile.mode == "disabled"
        ):
            raise DomainError(
                409, "model_unavailable", "A model check requires trusted configuration."
            )
        now = database_now(db)
        recent = db.scalar(select(ModelCheck).order_by(ModelCheck.created_at.desc()).limit(1))
        active = db.scalar(
            select(ModelCheck.id)
            .where(ModelCheck.state.in_(["queued", "in_flight"]), ModelCheck.deadline > now)
            .limit(1)
        )
        if active or (recent and recent.created_at + timedelta(seconds=60) > now):
            raise DomainError(409, "check_cooldown", "Wait for the current check and its cooldown.")
        check = ModelCheck(
            actor_id=actor_id,
            idempotency_key=key,
            identity=deployment.identity(),
            dispatch_epoch=dispatch_epoch(),
            deadline=now + timedelta(minutes=15),
        )
        db.add(check)
        db.flush()
        db.add(Job(kind=PROBE_JOB, subject_id=check.id, max_attempts=1))
        return check_view(check, deployment)
