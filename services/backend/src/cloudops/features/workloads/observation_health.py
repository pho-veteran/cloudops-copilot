"""Read a single configured target and its bounded, persisted E02 evidence."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, select

from cloudops.features.workloads.models import ScopeState, ScopeTarget, Workload
from cloudops.features.workloads.observation_models import ObservationState, WorkloadObservation
from cloudops.features.workloads.observation_reader import reader_bindings
from cloudops.features.workloads.observation_service import observation_binding, summary
from cloudops.features.workloads.observation_schemas import ObservationSummary
from cloudops.features.workloads.observation_settings import POLICY
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
    last_success_config_id: str | None = None
    success_matches: bool = False
    failure_matches: bool = False
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
    reader, public_reader = reader_bindings()
    evidence.reader_configured = reader is not None
    try:
        _, current_digest = load_scope(settings().workloads_file)
    except (DomainError, ValidationError):
        return evidence
    evidence.config_matches_scope = bool(scope and scope.config_digest == current_digest)
    if not evidence.config_matches_scope:
        return evidence
    epoch = workload.observation_epoch
    evidence.config_id = observation_binding(public_reader, current_digest, epoch)
    binding = observation_binding(reader, current_digest, epoch)
    state = evidence.state
    if not state:
        return evidence
    if state.last_success_id and state.bound_success_id == state.last_success_id:
        evidence.last_success_config_id = state.bound_success_config_id
        evidence.success_matches = bool(binding and state.success_binding == binding)
    evidence.failure_matches = bool(
        binding
        and state.failure_binding == binding
        and state.bound_failure_at == state.last_failure_at
    )
    if (
        binding
        and state.attempt_binding == binding
        and state.bound_attempt_sequence == state.sequence
    ):
        success = evidence.success
        if not evidence.success_matches or (
            success and success.observed_at < now - timedelta(days=POLICY.history_days)
        ):
            success = None
        evidence.assessment = summary(workload, state, success, now)
    return evidence
