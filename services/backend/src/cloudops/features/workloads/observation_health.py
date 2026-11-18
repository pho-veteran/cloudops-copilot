"""Read a single configured target and its bounded, persisted E02 evidence."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, select

from cloudops.features.workloads.models import ScopeState, ScopeTarget, Workload
from cloudops.features.workloads.observation_models import ObservationState, WorkloadObservation
from cloudops.features.workloads.observation_reader import reader_binding
from cloudops.features.workloads.observation_service import observation_binding, summary
from cloudops.features.workloads.observation_schemas import ObservationSummary
from cloudops.features.workloads.scope_config import load_scope
from cloudops.shared.config import settings
from cloudops.shared.errors import DomainError


@dataclass
class ScopedEvidence:
    workload: Workload | None = None
    target: ScopeTarget | None = None
    state: ObservationState | None = None
    success: WorkloadObservation | None = None
    config_id: str | None = None
    reader_configured: bool = False
    config_matches_scope: bool = False
    assessment: ObservationSummary | None = None


def scoped_evidence(db, now: datetime) -> ScopedEvidence:
    scope = db.get(ScopeState, 1)
    query = select(Workload, ObservationState, WorkloadObservation)
    if scope and scope.mode == "v2":
        query = (
            query.add_columns(ScopeTarget)
            .join_from(ScopeTarget, Workload, ScopeTarget.workload_id == Workload.id, isouter=True)
            .where(ScopeTarget.configured, ScopeTarget.cluster == scope.cluster_key)
            .order_by(ScopeTarget.namespace, ScopeTarget.name)
        )
    else:
        query = query.where(Workload.active).order_by(
            Workload.source.desc(), Workload.cluster, Workload.namespace, Workload.name, Workload.id
        )
    selected = db.execute(
        query.join_from(
            Workload, ObservationState, Workload.id == ObservationState.workload_id, isouter=True
        )
        .outerjoin(
            WorkloadObservation,
            and_(
                ObservationState.last_success_id == WorkloadObservation.id,
                ObservationState.workload_id == WorkloadObservation.workload_id,
            ),
        )
        .limit(1)
    ).first()
    evidence = ScopedEvidence()
    if selected:
        evidence.workload, evidence.state, evidence.success, *targets = selected
        if targets:
            evidence.target = targets[0]

    workload = evidence.workload
    if not workload or not workload.active or workload.source != "live_kubernetes":
        return evidence
    reader = reader_binding()
    evidence.reader_configured = reader is not None
    try:
        _, current_digest = load_scope(settings().workloads_file)
    except (DomainError, ValidationError):
        return evidence
    evidence.config_matches_scope = bool(scope and scope.config_digest == current_digest)
    if evidence.config_matches_scope:
        evidence.config_id = observation_binding(reader, current_digest, workload.observation_epoch)
    state = evidence.state
    if evidence.config_id and state and state.attempt_binding == evidence.config_id:
        matching_success = evidence.success if state.success_binding == evidence.config_id else None
        evidence.assessment = summary(workload, state, matching_success, now)
    return evidence
