"""Caller-transactional analysis admission and terminal transitions."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from cloudops.features.diagnosis import pins
from cloudops.features.diagnosis.models import Analysis, AnalysisCall
from cloudops.features.diagnosis.settings import load
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import database_now
from cloudops.shared.jobs import Job, retire_queued

ANALYSIS_JOB = "diagnosis_analysis_v1"
PROBE_JOB = "diagnosis_probe_v1"
ACTIVE = ("pending", "analyzing", "waiting_for_quota")
_dispatch_reader = None


def configure_dispatch(reader):
    global _dispatch_reader
    _dispatch_reader = reader


def dispatch_epoch():
    if _dispatch_reader is None:
        return {}
    permit, _, _ = _dispatch_reader.current()
    return (
        {"authority": str(permit.authority_id), "generation": permit.generation} if permit else {}
    )


def dispatch_status(kind):
    return _dispatch_reader.status(kind) if _dispatch_reader else (False, "permit_missing")


def admit(db, run: InvestigationRun, *, purpose="operational") -> Analysis | None:
    existing = db.scalar(
        select(Analysis).where(Analysis.run_id == run.id, Analysis.purpose == purpose)
    )
    if existing:
        return existing
    deployment = load()
    denied = deployment.denied(purpose)
    if denied:
        if purpose == "operational":
            run.state, run.outcome_code = "insufficient_evidence", "analysis_unavailable"
            run.reason = "Evidence saved. Analysis unavailable: " + denied + "."
            run.finished_at = database_now(db)
        return None
    now = database_now(db)
    analysis = Analysis(
        id=uuid4(),
        run_id=run.id,
        purpose=purpose,
        identity=deployment.identity(),
        dispatch_epoch=dispatch_epoch(),
        deadline=now + timedelta(minutes=15),
    )
    db.add(analysis)
    db.flush()
    db.add(Job(kind=ANALYSIS_JOB, subject_id=analysis.id))
    if purpose == "operational":
        run.state, run.provider_call, run.outcome_code = "analyzing", "none", None
        run.reason, run.finished_at = "Evidence saved. Waiting for bounded analysis.", None
    return analysis


def terminal(db, analysis, run, state, reason):
    analysis.state, analysis.reason = state, reason
    analysis.wait_mode, analysis.earliest_retry = None, None
    analysis.finished_at = database_now(db)
    if reason == "provider_outcome_unknown":
        for call in db.scalars(
            select(AnalysisCall).where(
                AnalysisCall.analysis_id == analysis.id, AnalysisCall.settled_at.is_(None)
            )
        ):
            call.settled_at = analysis.finished_at
            call.receipt = {
                "kind": "provider_outcome_unknown",
                "local_reconciliation": True,
                "usage": None,
                "usage_status": "unavailable",
            }
    if analysis.purpose == "operational" and run.state != "cancelled":
        run.state = (
            "completed"
            if state == "completed"
            else ("insufficient_evidence" if state == "insufficient_evidence" else "failed")
        )
        run.reason = "Analysis finished: " + reason + "."
        run.outcome_code = (
            "analysis_completed"
            if state == "completed"
            else "analysis_insufficient"
            if state == "insufficient_evidence"
            else "analysis_failed"
        )
        run.provider_call, run.finished_at = None, analysis.finished_at
    pins.release(db, analysis.id)


def cancel_unsent(db, run, reason):
    """Run lock precedes analysis; never wait for a Job locked by a claimer."""
    analyses = db.scalars(
        select(Analysis)
        .where(Analysis.run_id == run.id, Analysis.state.in_(ACTIVE))
        .order_by(Analysis.id)
        .with_for_update()
    ).all()
    for analysis in analyses:
        pending = db.scalar(
            select(AnalysisCall.id).where(
                AnalysisCall.analysis_id == analysis.id, AnalysisCall.settled_at.is_(None)
            )
        )
        if pending:
            continue
        analysis.state, analysis.reason = "cancelled", reason
        analysis.finished_at = database_now(db)
        pins.release(db, analysis.id)
        retire_queued(db, ANALYSIS_JOB, analysis.id, reason)
        held = db.scalar(
            select(Job)
            .where(Job.kind == ANALYSIS_JOB, Job.subject_id == analysis.id, Job.state == "held")
            .with_for_update(skip_locked=True)
        )
        if held:
            held.state, held.error = "cancelled", reason
