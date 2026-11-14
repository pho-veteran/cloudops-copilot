from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from diagnosis_fixtures import profile
from knowledge_fixtures import document, write_corpus
from sqlalchemy import select

from cloudops.features.access.models import User
from cloudops.features.diagnosis.context import (
    InsufficientContext,
    PreparedContext,
    assemble,
    build,
    family_matrix,
    load_materials,
    resolve_scope,
    usability,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.knowledge.guidance import pin_guidance
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.knowledge.models import KnowledgeSelection
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import sessions

STATE = {"pods": [{"uid": "pod-1", "phase": "Running"}]}
EVENTS = {"events": [{"uid": "event-1", "reason": "BackOff", "lastTimestamp": 1}]}
LOGS = {"pods": [{"pod_uid": "pod-1", "container": "app", "lines": ["started"]}]}
SERIES = {"series": [{"metric": "request_rate", "samples": [[1, 2.0]]}]}
FIXTURE = {"label": "Synthetic development evidence; no live cluster was contacted."}

ALL_CLASSES = [
    "operator_request",
    "kubernetes_state",
    "kubernetes_events",
    "container_logs",
    "prometheus_series",
    "runbook",
    "synthetic_fixture",
    "diagnostic_fixture",
]


def policy(*classes: str) -> EgressPolicy:
    return EgressPolicy.model_validate(
        {
            "schema": "EgressPolicy/v1",
            "policy_id": "test-policy",
            "policy_version": "1",
            "destination": {
                "config_id": "test-config",
                "config_version": "1",
                "endpoint_label": "loopback",
            },
            "allowed_source_classes": list(classes) if classes else ALL_CLASSES,
            "max_message_bytes": 65536,
            "max_target_bytes": 8192,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": [],
        }
    )


@pytest.fixture
def context(db, seeded):
    actor = db.scalar(select(User).where(User.username == "operator"))
    workload = db.scalar(select(Workload))
    incident = Incident(
        workload_id=workload.id,
        title="Crash loop",
        description="Observed restarts",
        created_by=actor.id,
    )
    db.add(incident)
    db.flush()
    now = datetime.now(timezone.utc)
    run = InvestigationRun(
        incident_id=incident.id,
        state="insufficient_evidence",
        reason="Analysis unavailable",
        requested_by=actor.id,
        question="Why is the workload in CrashLoopBackOff?",
        question_source="operator",
        window_start=now - timedelta(minutes=30),
        window_end=now,
        window_source="explicit",
        pack_coverage=[
            {
                "pack": "crashloop_startup",
                "status": "available",
                "missing_required": [],
                "optional_gaps": [],
            }
        ],
    )
    db.add(run)
    db.commit()
    return incident.id, run.id, workload.uid


def add_evidence(
    db,
    context,
    source_key,
    payload,
    *,
    coverage="available",
    reason_codes=(),
    content="available",
    time_relation="within_requested_window",
    expires_at=None,
    payload_version=1,
):
    _, run_id, uid = context
    spec = SOURCES[source_key]
    now = datetime.now(timezone.utc)
    available = content == "available"
    row = Evidence(
        run_id=run_id,
        workload_uid=uid,
        source_key=source_key,
        source_kind=spec.source_kind,
        catalog_version=CATALOG_VERSION,
        schema_version=2,
        payload_kind=spec.payload_kind,
        payload_version=payload_version,
        collected_at=now,
        time_relation=time_relation,
        coverage={"status": coverage, "reason_codes": list(reason_codes)},
        redaction_policy_version="redaction/v1",
        expires_at=expires_at,
        sha256=None if content == "unavailable" else "a" * 64,
        payload=payload if available else None,
    )
    db.add(row)
    db.commit()
    return row


def materials(db, context, *classes):
    scope = resolve_scope(db, context[0], context[1])
    assert scope is not None
    return scope, {material.slot: material for material in load_materials(db, scope)}


def test_scope_matches_persisted_run_and_workload(db, context):
    scope = resolve_scope(db, context[0], context[1])
    run = db.get(InvestigationRun, context[1])
    incident = db.get(Incident, context[0])
    workload = db.get(Workload, incident.workload_id)
    assert scope is not None
    assert scope.knowledge.incident_id == context[0]
    assert scope.knowledge.run_id == context[1]
    assert scope.knowledge.workload_id == workload.id
    assert scope.knowledge.workload_uid == workload.uid
    assert scope.knowledge.cluster == workload.cluster
    assert scope.knowledge.namespace == workload.namespace
    assert scope.knowledge.name == workload.name
    assert scope.knowledge.kind == "Deployment"
    assert scope.knowledge.window_start == run.window_start
    assert scope.knowledge.window_end == run.window_end
    assert scope.request_text == run.question
    assert scope.request_source == "operator"
    assert scope.window_source == "explicit"


@pytest.mark.parametrize("change", ["question", "not_captured", "window", "workload"])
def test_legacy_run_without_request_or_window_has_no_scope(db, context, change):
    _, run_id, _ = context
    run = db.get(InvestigationRun, run_id)
    if change == "question":
        run.question = None
    elif change == "not_captured":
        run.question_source = "not_captured"
    elif change == "window":
        run.window_start = None
        run.window_end = None
    else:
        workload = db.get(Workload, db.get(Incident, context[0]).workload_id)
        workload.namespace = ""
    db.commit()
    assert resolve_scope(db, context[0], context[1]) is None


def test_coverage_separates_source_and_content_status(db, context):
    now = datetime.now(timezone.utc)
    add_evidence(db, context, "kubernetes.state", STATE)
    add_evidence(db, context, "kubernetes.events", EVENTS, coverage="partial")
    add_evidence(
        db,
        context,
        "kubernetes.logs.current",
        LOGS,
        coverage="unavailable",
        reason_codes=["source_not_configured"],
        content="unavailable",
    )
    add_evidence(db, context, "kubernetes.logs.previous", None, coverage="not_applicable")
    add_evidence(
        db,
        context,
        "kubernetes.configmap.metadata",
        {"configmaps": [{"uid": "cm-1"}]},
        content="expired",
        expires_at=now - timedelta(days=1),
    )
    _, slot = materials(db, context)
    assert slot["kubernetes.state"].coverage_status == "available"
    assert slot["kubernetes.state"].content_status == "available"
    assert slot["kubernetes.state"].entries
    assert slot["kubernetes.events"].coverage_status == "partial"
    assert slot["kubernetes.logs.current"].coverage_status == "unavailable"
    assert slot["kubernetes.logs.current"].coverage_reason_codes == ("source_not_configured",)
    assert slot["kubernetes.logs.current"].content_status == "unavailable"
    assert slot["kubernetes.logs.previous"].coverage_status == "not_applicable"
    assert slot["kubernetes.configmap.metadata"].content_status == "expired"
    assert slot["kubernetes.configmap.metadata"].entries == ()
    assert slot["prometheus.request_rate"].coverage_status == "missing"
    assert slot["prometheus.request_rate"].content_status == "missing"
    assert slot["prometheus.request_rate"].evidence_id is None


def test_partial_is_usable_but_blocks_only_ruled_out(db, context):
    add_evidence(db, context, "kubernetes.state", STATE, coverage="partial")
    add_evidence(db, context, "kubernetes.events", EVENTS)
    add_evidence(db, context, "kubernetes.logs.current", LOGS)
    add_evidence(db, context, "kubernetes.logs.previous", LOGS)
    _, slot = materials(db, context)
    usable, waived, reasons = usability(slot["kubernetes.state"], policy())
    assert (usable, waived) == (True, False)
    assert "source_partial" in reasons
    families = {item.family: item for item in family_matrix(list(slot.values()), policy())}
    assert families["crashloop_startup"].eligible_for_assessment is True
    assert families["crashloop_startup"].partial is True


def test_development_fixture_never_satisfies_a_required_slot(db, context):
    add_evidence(db, context, "development_fixture", FIXTURE)
    _, slot = materials(db, context)
    assert slot["development_fixture"].entries
    usable, _, reasons = usability(slot["development_fixture"], policy())
    assert usable is False
    assert "synthetic_fixture_not_production_evidence" in reasons
    for family in family_matrix(list(slot.values()), policy()):
        assert family.eligible_for_assessment is False
        assert "synthetic_fixture_not_production_evidence" in family.reasons
        assert all(required.targets == 0 for required in family.required_slots)


def test_missing_traffic_leaves_kubernetes_families_eligible(db, context):
    add_evidence(db, context, "kubernetes.state", STATE)
    add_evidence(db, context, "kubernetes.events", EVENTS)
    add_evidence(db, context, "kubernetes.logs.current", LOGS)
    add_evidence(db, context, "kubernetes.logs.previous", LOGS)
    _, slot = materials(db, context)
    families = {item.family: item for item in family_matrix(list(slot.values()), policy())}
    for name in ("crashloop_startup", "oom_restarts", "pending_unschedulable", "image_pull"):
        assert families[name].eligible_for_assessment is True
    traffic = families["traffic_latency_errors"]
    assert traffic.eligible_for_assessment is False
    assert "required_slot_unavailable" in traffic.reasons


def test_ambiguous_entries_are_counted_and_excluded(db, context):
    payload = {
        "pods": [
            {"uid": "pod-1", "phase": "Running"},
            {"uid": "pod-1", "phase": "Pending"},
            {"uid": "pod-2", "phase": "Running"},
        ]
    }
    add_evidence(db, context, "kubernetes.state", payload)
    _, slot = materials(db, context)
    state = slot["kubernetes.state"]
    assert state.ambiguous_omitted > 0
    assert state.entries
    assert all(entry.ambiguous is False for entry in state.entries)


def test_outside_window_is_a_limitation_not_a_closed_interval(db, context):
    add_evidence(
        db,
        context,
        "kubernetes.events",
        EVENTS,
        time_relation="extends_after_window",
    )
    _, slot = materials(db, context)
    assert slot["kubernetes.events"].time_relation == "extends_after_window"


def test_no_family_is_inferred_from_question_or_pack_coverage(db, context):
    assert "CrashLoopBackOff" in db.get(InvestigationRun, context[1]).question
    _, slot = materials(db, context)
    families = family_matrix(list(slot.values()), policy())
    assert [item.family for item in families] == [
        "crashloop_startup",
        "oom_restarts",
        "traffic_latency_errors",
        "pending_unschedulable",
        "image_pull",
    ]
    assert all(item.eligible_for_assessment is False for item in families)


def _crashloop_evidence(db, context):
    add_evidence(db, context, "kubernetes.state", STATE)
    add_evidence(db, context, "kubernetes.events", EVENTS)
    add_evidence(db, context, "kubernetes.logs.current", LOGS)
    add_evidence(db, context, "kubernetes.logs.previous", LOGS)


def _build(db, context, selection=None):
    return build(
        db,
        context[0],
        context[1],
        selection=selection,
        policy=policy(),
        profile=profile(),
        analysis_identity="analysis-1",
    )


def test_build_over_persisted_run_matches_the_pure_assembly(db, context):
    _crashloop_evidence(db, context)
    scope = resolve_scope(db, context[0], context[1])
    assert scope is not None
    execution_policy = policy()
    pure = assemble(
        load_materials(db, scope),
        scope,
        None,
        execution_policy,
        profile(),
        "analysis-1",
    )
    rebuilt = _build(db, context)
    assert isinstance(pure, PreparedContext)
    assert isinstance(rebuilt, PreparedContext)
    assert rebuilt.digest == pure.digest
    assert rebuilt.message_bytes == pure.message_bytes
    assert rebuilt.incident_id == context[0]
    assert rebuilt.run_id == context[1]
    assert [family.family for family in rebuilt.families] == [
        family.family for family in pure.families
    ]

    run = db.get(InvestigationRun, context[1])
    run.question = None
    run.question_source = "not_captured"
    db.commit()
    legacy = _build(db, context)
    assert isinstance(legacy, InsufficientContext)
    assert legacy.reason == "scope_unavailable"
    assert legacy.source_coverage == []
    assert legacy.families == []


def test_analysis_selection_is_independent_of_manual_selection(db, context, tmp_path):
    _crashloop_evidence(db, context)
    run = db.get(InvestigationRun, context[1])
    run.question = "unmatchedqueryxyz"
    db.commit()
    write_corpus(
        tmp_path,
        [document(title="Memory guide")],
        {"fixture-guide": "# Memory guide\n\nMemory pressure and restarts.\n"},
    )
    assert index_corpus(tmp_path).outcome in {"indexed", "unchanged", "reactivated"}
    actor = db.scalar(select(User).where(User.username == "operator"))
    with sessions()() as session, session.begin():
        manual = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="manual",
            request_identity="manual",
            actor_id=actor.id,
        )
        assert manual.selection.retrieval.status == "no_matches"
        analysis = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="analysis",
            request_identity="analysis-1",
            query="memory",
        )
        assert analysis.selection.retrieval.status == "matched"
        assert analysis.selection.id != manual.selection.id
        other_analysis = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="analysis",
            request_identity="analysis-2",
            query="memory",
        )
    db.expire_all()
    for mismatched in (manual.selection, other_analysis.selection):
        rejected = _build(db, context, mismatched)
        assert isinstance(rejected, InsufficientContext)
        assert rejected.reason == "analysis_selection_mismatch"
    first = _build(db, context, analysis.selection)
    assert isinstance(first, PreparedContext)
    assert first.selection_id == analysis.selection.id
    assert first.runbook_status == "matched"
    assert [(item.alias, item.reference.document_key) for item in first.runbooks] == [
        ("K001", "fixture-guide")
    ]

    with sessions()() as session, session.begin():
        manual_row = session.scalar(
            select(KnowledgeSelection).where(
                KnowledgeSelection.run_id == context[1],
                KnowledgeSelection.consumer == "manual",
                KnowledgeSelection.request_identity == "manual",
            )
        )
        assert manual_row is not None
        session.delete(manual_row)
        session.flush()
        replacement = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="manual",
            request_identity="manual",
            actor_id=actor.id,
        )
        assert replacement.selection.id != manual.selection.id
        again = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="analysis",
            request_identity="analysis-1",
            query="memory",
        )
        assert again.selection.id == analysis.selection.id
    db.expire_all()
    second = _build(db, context, again.selection)
    assert isinstance(second, PreparedContext)
    assert second.digest == first.digest


@pytest.mark.parametrize(
    "field,value", [("workload_uid", "another-workload"), ("catalog_version", "unsupported/v999")]
)
def test_build_excludes_evidence_outside_the_resolved_identity(db, context, field, value):
    _crashloop_evidence(db, context)
    row = db.scalar(
        select(Evidence).where(
            Evidence.run_id == context[1], Evidence.source_key == "kubernetes.state"
        )
    )
    setattr(row, field, value)
    db.commit()
    assert isinstance(_build(db, context), InsufficientContext)


def test_supported_catalog_is_not_overwritten_by_another_catalog(db, context):
    _crashloop_evidence(db, context)
    row = add_evidence(
        db, context, "kubernetes.configmap.metadata", {"metadata": {"name": "foreign-catalog"}}
    )
    row.source_key = "kubernetes.state"
    row.catalog_version = "unsupported/v999"
    db.commit()
    result = _build(db, context)
    assert isinstance(result, PreparedContext)
    assert all(target.evidence_id != row.id for target in result.required_targets)
    assert "foreign-catalog" not in result.messages[1].content
