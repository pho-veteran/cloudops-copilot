from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from diagnosis_fixtures import profile
from knowledge_fixtures import document, write_corpus
from sqlalchemy import func, select

from cloudops.features.access.models import User
from cloudops.features.diagnosis.advice import feedback_message, validate
from cloudops.features.diagnosis.context import (
    InsufficientContext,
    PreparedContext,
    build,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, EvidencePin, InvestigationRun
from cloudops.features.knowledge.guidance import pin_guidance
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import sessions

STATE = {"pods": [{"uid": "pod-1", "phase": "Running"}]}
EVENTS = {"events": [{"uid": "event-1", "reason": "BackOff", "lastTimestamp": 1}]}
LOGS = {"pods": [{"pod_uid": "pod-1", "container": "app", "lines": ["started"]}]}
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


def _crashloop(db, context, *, expires_at=None):
    state = add_evidence(db, context, "kubernetes.state", STATE, expires_at=expires_at)
    add_evidence(db, context, "kubernetes.events", EVENTS)
    add_evidence(db, context, "kubernetes.logs.current", LOGS)
    add_evidence(db, context, "kubernetes.logs.previous", LOGS)
    return state


def _build(db, context, selection=None):
    built = build(
        db,
        context[0],
        context[1],
        selection=selection,
        policy=policy(),
        profile=profile(),
        analysis_identity="analysis-1",
    )
    assert isinstance(built, PreparedContext)
    return built


def _corpus(root):
    write_corpus(
        root,
        [document(title="Memory guide")],
        {"fixture-guide": "# Memory guide\n\nMemory pressure and restarts.\n"},
    )
    assert index_corpus(root).outcome in {"indexed", "unchanged", "reactivated"}


def _analysis_selection(db, context, root):
    _corpus(root)
    with sessions()() as session, session.begin():
        pinned = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="analysis",
            request_identity="analysis-1",
            query="memory",
        )
        assert pinned.selection.retrieval.status == "matched"
    db.expire_all()
    return pinned.selection


def _raw(**overrides):
    value = {
        "schema_version": "RCAAdvice/v1",
        "status": "supported",
        "cause_category": "crashloop_startup",
        "summary": "The workload crash-loops after startup.",
        "supporting_evidence_ids": ["E001"],
        "contradicting_evidence_ids": [],
        "alternatives": [],
        "ruled_out_hypotheses": [],
        "unverified_checks": [],
        "missing_evidence": [],
        "limitations": [],
        "next_steps": ["Inspect the previous container logs."],
        "runbook_citation_ids": [],
        "candidate_template_id": "restore_deployment_template",
    }
    value.update(overrides)
    return json.dumps(value)


def _codes(outcome):
    return [failure.code for failure in outcome.failures]


def test_supported_output_resolves_exact_evidence_and_runbook_refs(db, context, tmp_path):
    _crashloop(db, context)
    selection = _analysis_selection(db, context, tmp_path)
    built = _build(db, context, selection)
    assert built.runbooks
    outcome = validate(
        db,
        built,
        _raw(
            supporting_evidence_ids=["E001", "E002"],
            contradicting_evidence_ids=["E003"],
            runbook_citation_ids=["K001"],
        ),
    )
    assert outcome.status == "valid", outcome.failures
    result = outcome.result
    assert result is not None
    targets = {target.alias: target for target in built.required_targets}
    for resolved, alias in zip(result.supporting, ["E001", "E002"], strict=True):
        target = targets[alias]
        assert resolved.citation.run_id == context[1]
        assert resolved.citation.evidence_id == target.evidence_id
        assert resolved.citation.locator == target.locator
        assert resolved.source_key == target.source_key
        assert resolved.evidence_sha256 == target.evidence_sha256
        assert resolved.entry_digest == target.entry_digest
        assert resolved.time_relation == target.time_relation
        assert resolved.coverage_status == target.coverage_status
    runbook = built.runbooks[0]
    assert result.runbook_citations[0].selection_id == runbook.selection_id
    assert result.runbook_citations[0].reference == runbook.reference
    assert result.quality == "not_established"
    assert result.machine_validation == "structural_v1"


def test_swapped_locator_key_is_terminal(db, context):
    _crashloop(db, context)
    built = _build(db, context)
    targets = list(built.required_targets)
    swapped = targets[0].model_copy(update={"locator": targets[1].locator})
    broken = built.model_copy(update={"required_targets": [swapped, *targets[1:]]})
    outcome = validate(db, broken, _raw())
    assert outcome.status == "invalid"
    assert outcome.result is None
    assert _codes(outcome) == ["evidence_reference_mismatch"]
    assert outcome.repairable is False
    assert outcome.failures[0].path == "supporting_evidence_ids[0]"
    assert feedback_message(profile(), broken, outcome.failures) is None


def test_swapped_evidence_sha256_is_terminal(db, context):
    _crashloop(db, context)
    built = _build(db, context)
    row = db.get(Evidence, built.required_targets[0].evidence_id)
    row.sha256 = "b" * 64
    db.commit()
    outcome = validate(db, built, _raw())
    assert outcome.status == "invalid"
    assert _codes(outcome) == ["evidence_hash_mismatch"]
    assert outcome.repairable is False
    assert outcome.result is None


def test_purged_evidence_is_terminal(db, context):
    _crashloop(db, context)
    built = _build(db, context)
    row = db.get(Evidence, built.required_targets[0].evidence_id)
    row.payload = None
    db.commit()
    outcome = validate(db, built, _raw())
    assert outcome.status == "invalid"
    assert _codes(outcome) == ["evidence_expired"]
    assert outcome.repairable is False


def test_alias_from_another_run_does_not_exist(db, context):
    _crashloop(db, context)
    built = _build(db, context)
    now = datetime.now(timezone.utc)
    run = InvestigationRun(
        incident_id=context[0],
        state="insufficient_evidence",
        reason="Second run",
        requested_by=db.get(InvestigationRun, context[1]).requested_by,
        question="Why did the workload restart?",
        question_source="operator",
        window_start=now - timedelta(minutes=30),
        window_end=now,
        window_source="explicit",
        pack_coverage=[],
    )
    db.add(run)
    db.commit()
    other = add_evidence(db, (context[0], run.id, context[2]), "kubernetes.state", STATE)
    assert all(target.evidence_id != other.id for target in built.required_targets)
    assert all(target.evidence_id != other.id for target in built.optional_targets)
    outcome = validate(db, built, _raw(supporting_evidence_ids=["E999"]))
    assert outcome.status == "invalid"
    assert outcome.result is None
    assert _codes(outcome) == ["unknown_alias"]


@pytest.mark.parametrize(
    ("change", "code"),
    [("expired", "evidence_expired"), ("unavailable", "evidence_unavailable")],
)
def test_expired_or_unavailable_evidence_is_terminal(db, context, change, code):
    now = datetime.now(timezone.utc)
    row = _crashloop(db, context, expires_at=now + timedelta(hours=1))
    built = _build(db, context)
    if change == "expired":
        row.expires_at = now - timedelta(hours=1)
    else:
        row.sha256 = None
    db.commit()
    outcome = validate(db, built, _raw())
    assert outcome.status == "invalid"
    assert outcome.result is None
    assert _codes(outcome) == [code]
    assert outcome.repairable is False


def test_swapped_runbook_chunk_and_hash_are_terminal(db, context, tmp_path):
    _crashloop(db, context)
    selection = _analysis_selection(db, context, tmp_path)
    built = _build(db, context, selection)
    runbook = built.runbooks[0]
    for field in ("chunk_id", "document_version_id"):
        swapped = runbook.model_copy(
            update={"reference": runbook.reference.model_copy(update={field: uuid4()})}
        )
        broken = built.model_copy(update={"runbooks": [swapped, *built.runbooks[1:]]})
        outcome = validate(db, broken, _raw(runbook_citation_ids=["K001"]))
        assert outcome.status == "invalid"
        assert outcome.result is None
        assert _codes(outcome) == ["runbook_reference_mismatch"]
        assert outcome.repairable is False
    unknown = validate(db, built, _raw(runbook_citation_ids=["K999"]))
    assert unknown.status == "invalid"
    assert unknown.result is None
    assert _codes(unknown) == ["runbook_alias_unknown"]


def test_pinned_old_runbook_resolves_after_reindex(db, context, tmp_path):
    _crashloop(db, context)
    selection = _analysis_selection(db, context, tmp_path)
    built = _build(db, context, selection)
    old = built.runbooks[0].reference
    write_corpus(
        tmp_path,
        [document(title="Memory guide", version="2")],
        {"fixture-guide": "# Memory guide\n\nMemory pressure and restarts, revised.\n"},
    )
    assert index_corpus(tmp_path).outcome in {"indexed", "unchanged", "reactivated"}
    outcome = validate(db, built, _raw(runbook_citation_ids=["K001"]))
    assert outcome.status == "valid", outcome.failures
    resolved = outcome.result.runbook_citations[0].reference
    assert resolved.document_version_id == old.document_version_id
    assert resolved.chunk_id == old.chunk_id
    assert resolved.version == "1"
    assert resolved == old


def test_fixture_only_run_is_insufficient_and_never_supported(db, context):
    add_evidence(db, context, "development_fixture", FIXTURE)
    built = build(
        db,
        context[0],
        context[1],
        selection=None,
        policy=policy(),
        profile=profile(),
        analysis_identity="analysis-1",
    )
    assert isinstance(built, InsufficientContext)
    assert built.reason == "no_eligible_family"
    assert all(not family.eligible_for_assessment for family in built.families)
    assert built.omissions == []


def test_validation_is_read_only(db, context, tmp_path):
    _crashloop(db, context)
    selection = _analysis_selection(db, context, tmp_path)
    built = _build(db, context, selection)
    counts = {
        "evidence": db.scalar(select(func.count()).select_from(Evidence)),
        "pins": db.scalar(select(func.count()).select_from(EvidencePin)),
    }
    outcome = validate(db, built, _raw(runbook_citation_ids=["K001"]))
    assert outcome.status == "valid", outcome.failures
    assert db.scalar(select(func.count()).select_from(Evidence)) == counts["evidence"]
    assert db.scalar(select(func.count()).select_from(EvidencePin)) == counts["pins"]
    assert counts["pins"] == 0
