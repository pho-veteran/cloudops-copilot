from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.schemas import ReportPage, ReportView
from cloudops.features.recommendations.service import (
    REPORT_JOB,
    decode_cursor,
    encode_cursor,
    feature_observation,
    prepare_report,
    report_view,
)
from cloudops.features.workloads.service import require_active
from cloudops.shared.config import settings
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.jobs import Job

router = APIRouter(
    prefix="/api/recommendation-reports", tags=["recommendations"], responses=ERROR_RESPONSES
)


class ReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_id: UUID
    requested_hours: int = Field(default=6, ge=6, le=24)


@router.post(
    "", operation_id="create_recommendation_report", response_model=ReportView, status_code=201
)
def create(body: ReportBody, actor: Principal = Depends(operator), db: Session = Depends(get_db)):
    with db.begin():
        lock_admission(db)
        workload = require_active(db, body.workload_id)
        if workload.source != "live_kubernetes":
            raise DomainError(
                409,
                "live_target_required",
                "KRR requires a live configured workload; synthetic recommendations are unavailable.",
            )
        if db.scalar(
            select(RecommendationReport.id).where(
                RecommendationReport.workload_id == workload.id,
                RecommendationReport.schema_version == 2,
                RecommendationReport.state.in_(["queued", "running"]),
            )
        ):
            raise DomainError(
                409, "report_active", "A report is already admitted for this workload."
            )
        if (
            db.scalar(
                select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))
            )
            >= settings().queue_limit
        ):
            raise DomainError(409, "queue_full", "The bounded work queue is full.")
        report = prepare_report(db, workload, actor.id, body.requested_hours)
        db.add(report)
        db.flush()
        db.add(Job(kind=REPORT_JOB, subject_id=report.id))
        return report_view(db, report)


def get_report(db, report_id):
    row = db.get(RecommendationReport, report_id)
    if not row:
        raise DomainError(404, "report_not_found", "Recommendation report not found.")
    return row


@router.get(
    "",
    operation_id="list_recommendation_reports",
    response_model=ReportPage,
    dependencies=[Depends(current_session)],
)
def reports(
    workload_id: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
):
    query = select(RecommendationReport)
    if workload_id:
        query = query.where(RecommendationReport.workload_id == workload_id)
    if cursor:
        anchor = decode_cursor(cursor, workload_id)
        query = query.where(
            tuple_(RecommendationReport.created_at, RecommendationReport.id) < anchor
        )
    rows = db.scalars(
        query.order_by(
            RecommendationReport.created_at.desc(), RecommendationReport.id.desc()
        ).limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return ReportPage(
        items=[report_view(db, row) for row in rows],
        next_cursor=encode_cursor(rows[-1], workload_id) if more else None,
        observation=feature_observation(db),
    )


@router.get(
    "/{report_id}",
    operation_id="get_recommendation_report",
    response_model=ReportView,
    dependencies=[Depends(current_session)],
)
def report(report_id: UUID, db: Session = Depends(get_db)):
    return report_view(db, get_report(db, report_id))


@router.get(
    "/{report_id}/export",
    operation_id="export_recommendation_report",
    response_class=Response,
    dependencies=[Depends(current_session)],
)
def export(report_id: UUID, db: Session = Depends(get_db)):
    row = get_report(db, report_id)
    if row.export_text is None:
        raise DomainError(409, "report_not_final", "This report has no finalized export yet.")
    return Response(
        row.export_text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="resource-report-{report_id}.json"'},
    )
