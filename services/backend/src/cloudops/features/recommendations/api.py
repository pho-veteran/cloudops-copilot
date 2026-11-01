from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.workloads.service import require_active
from cloudops.shared.config import settings
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.jobs import Job
from cloudops.shared.schemas import View

router = APIRouter(
    prefix="/api/recommendation-reports", tags=["recommendations"], responses=ERROR_RESPONSES
)


class ReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_id: UUID
    requested_hours: int = Field(default=6, ge=6, le=24)


class ReportView(View):
    id: UUID
    workload_id: UUID
    state: Literal["queued", "running", "available", "insufficient_data", "unavailable"]
    requested_hours: int
    tool_version: str
    result: dict
    reason: str
    created_by: UUID
    created_at: datetime
    finished_at: datetime | None


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
        report = RecommendationReport(
            workload_id=body.workload_id, requested_hours=body.requested_hours, created_by=actor.id
        )
        db.add(report)
        db.flush()
        db.add(Job(kind="recommendation_report_v1", subject_id=report.id))
        return ReportView.model_validate(report)


def get_report(db, report_id):
    row = db.get(RecommendationReport, report_id)
    if not row:
        raise DomainError(404, "report_not_found", "Recommendation report not found.")
    return row


@router.get(
    "",
    operation_id="list_recommendation_reports",
    response_model=list[ReportView],
    dependencies=[Depends(current_session)],
)
def reports(db: Session = Depends(get_db)):
    return db.scalars(
        select(RecommendationReport).order_by(RecommendationReport.created_at.desc()).limit(50)
    ).all()


@router.get(
    "/{report_id}",
    operation_id="get_recommendation_report",
    response_model=ReportView,
    dependencies=[Depends(current_session)],
)
def report(report_id: UUID, db: Session = Depends(get_db)):
    return get_report(db, report_id)


@router.get(
    "/{report_id}/export",
    operation_id="export_recommendation_report",
    response_class=JSONResponse,
    dependencies=[Depends(current_session)],
)
def export(report_id: UUID, db: Session = Depends(get_db)):
    value = ReportView.model_validate(get_report(db, report_id)).model_dump(mode="json")
    return JSONResponse(
        value,
        headers={"Content-Disposition": f'attachment; filename="resource-report-{report_id}.json"'},
    )
