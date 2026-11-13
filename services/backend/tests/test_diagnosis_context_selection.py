from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from cloudops.features.access.models import User
from cloudops.features.diagnosis.context import (
    family_matrix,
    load_materials,
    resolve_scope,
    usability,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.workloads.models import Workload

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
