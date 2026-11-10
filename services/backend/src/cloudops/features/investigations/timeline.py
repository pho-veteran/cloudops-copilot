"""Read-only timeline projection of persisted rollout, activity and execution records."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.schemas import RequestWindow
from cloudops.features.investigations.timeline_schemas import RunTimeline, TimelineMarker
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.observation_models import WorkloadObservation
from cloudops.features.workloads.observation_schemas import Observation
from cloudops.shared.errors import DomainError

MARKER_LIMIT = 500
OBSERVATION_LIMIT = 200
ACTIVITY_LIMIT = 200
EXECUTION_LIMIT = 200
RESPONSE_BYTES = 128 * 1024
LIMITATIONS = (
    "Markers are persisted records aligned by time; alignment is correlation, not causality."
)


def _text(value: str, limit: int = 240) -> str:
    cleaned = " ".join(
        "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split()
    )
    return cleaned[:limit]


def _observation_marker(row: WorkloadObservation) -> TimelineMarker | None:
    try:
        value = Observation.model_validate(row.payload)
    except ValidationError:
        return None
    return TimelineMarker(
        kind="rollout_observation",
        at=row.observed_at,
        id=row.id,
        label="Rollout observation",
        detail={
            "health": value.health,
            "coverage": value.coverage,
            "generation": value.deployment.generation,
        },
    )


def run_timeline(db: Session, incident_id: UUID, run_id: UUID) -> RunTimeline:
    """Return one bounded timeline; it never invents a missing window or a marker."""
    run = db.scalar(
        select(InvestigationRun).where(
            InvestigationRun.id == run_id,
            InvestigationRun.incident_id == incident_id,
        )
    )
    if run is None:
        raise DomainError(404, "run_not_found", "Investigation not found for this incident.")
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise DomainError(404, "incident_not_found", "Incident not found.")
    if run.window_start is None or run.window_end is None:
        return RunTimeline(
            run_id=run.id,
            window=None,
            markers=[],
            truncated=False,
            limitations="Run window was not captured; no timeline marker is inferred.",
        )

    start, end = run.window_start, run.window_end
    markers = [
        TimelineMarker(
            kind="window_start",
            at=start,
            id=run.id,
            label="Captured window start",
        ),
        TimelineMarker(
            kind="window_end",
            at=end,
            id=run.id,
            label="Captured window end",
        ),
    ]

    observations = db.scalars(
        select(WorkloadObservation)
        .where(
            WorkloadObservation.workload_id == incident.workload_id,
            WorkloadObservation.observed_at >= start,
            WorkloadObservation.observed_at <= end,
        )
        .order_by(WorkloadObservation.observed_at, WorkloadObservation.id)
        .limit(OBSERVATION_LIMIT + 1)
    ).all()
    markers.extend(
        marker
        for row in observations[:OBSERVATION_LIMIT]
        if (marker := _observation_marker(row)) is not None
    )

    activities = db.scalars(
        select(IncidentActivity)
        .where(
            IncidentActivity.incident_id == incident_id,
            IncidentActivity.created_at >= start,
            IncidentActivity.created_at <= end,
        )
        .order_by(IncidentActivity.created_at, IncidentActivity.id)
        .limit(ACTIVITY_LIMIT + 1)
    ).all()
    markers.extend(
        TimelineMarker(
            kind="incident_activity",
            at=row.created_at,
            id=row.id,
            label=_text(row.kind),
            detail={"text": _text(row.text), "actor_id": str(row.actor_id)},
        )
        for row in activities[:ACTIVITY_LIMIT]
    )

    executions = db.execute(
        select(Execution, ActionPlan)
        .join(ActionPlan, Execution.plan_id == ActionPlan.id)
        .where(
            ActionPlan.incident_id == incident_id,
            or_(
                Execution.created_at.between(start, end),
                Execution.finished_at.between(start, end),
            ),
        )
        .order_by(Execution.created_at, Execution.id)
        .limit(EXECUTION_LIMIT + 1)
    ).all()
    source_truncated = (
        len(observations) > OBSERVATION_LIMIT
        or len(activities) > ACTIVITY_LIMIT
        or len(executions) > EXECUTION_LIMIT
    )
    for execution, _plan in executions[:EXECUTION_LIMIT]:
        if execution.created_at is not None and start <= execution.created_at <= end:
            markers.append(
                TimelineMarker(
                    kind="execution_created",
                    at=execution.created_at,
                    id=execution.id,
                    label="Recovery execution created",
                    detail={"plan_id": str(execution.plan_id), "state": execution.state},
                )
            )
        if execution.finished_at is not None and start <= execution.finished_at <= end:
            markers.append(
                TimelineMarker(
                    kind="execution_finished",
                    at=execution.finished_at,
                    id=execution.id,
                    label="Recovery execution finished",
                    detail={"plan_id": str(execution.plan_id), "state": execution.state},
                )
            )

    window_markers = [marker for marker in markers if marker.kind in {"window_start", "window_end"}]
    other_markers = [
        marker for marker in markers if marker.kind not in {"window_start", "window_end"}
    ]
    other_markers.sort(key=lambda marker: (marker.at, marker.kind, str(marker.id)))
    other_limit = max(0, MARKER_LIMIT - len(window_markers))
    truncated = source_truncated or len(other_markers) > other_limit
    markers = sorted(
        window_markers + other_markers[:other_limit],
        key=lambda marker: (marker.at, marker.kind, str(marker.id)),
    )
    result = RunTimeline(
        run_id=run.id,
        window=RequestWindow(start=start, end=end, source=run.window_source),
        markers=markers,
        truncated=truncated,
        limitations=LIMITATIONS,
    )
    while (
        len(result.markers) > len(window_markers)
        and len(result.model_dump_json().encode("utf-8")) > RESPONSE_BYTES
    ):
        for index in range(len(result.markers) - 1, -1, -1):
            if result.markers[index].kind not in {"window_start", "window_end"}:
                result.markers.pop(index)
                result.truncated = True
                break
        else:
            break
    return result
