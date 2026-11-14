from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from diagnosis_fixtures import profile
from sqlalchemy import event, select

from cloudops.features.access.models import User
from cloudops.features.diagnosis.context import PreparedContext, build
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.pins import PinDenied, acquire, release
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, EvidencePin, InvestigationRun
from cloudops.features.investigations.retention import purge_expired
from cloudops.features.investigations.service import evidence_content
from cloudops.features.workloads.models import Workload
from cloudops.shared.errors import DomainError

STATE = {"pods": [{"uid": "pod-1", "phase": "Running"}]}
EVENTS = {"events": [{"uid": "event-1", "reason": "BackOff", "lastTimestamp": 1}]}
LOGS = {"pods": [{"pod_uid": "pod-1", "container": "app", "lines": ["started"]}]}
CONFIGMAPS = {"configmaps": [{"uid": "cm-1", "name": "app-config"}]}

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
        payload_version=1,
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


def _context(db, context, *, expires_at=None):
    state = add_evidence(db, context, "kubernetes.state", STATE, expires_at=expires_at)
    add_evidence(db, context, "kubernetes.events", EVENTS)
    add_evidence(db, context, "kubernetes.logs.current", LOGS)
    add_evidence(db, context, "kubernetes.logs.previous", LOGS)
    return state


def _configmaps(db, context, *, expires_at=None):
    return add_evidence(
        db, context, "kubernetes.configmap.metadata", CONFIGMAPS, expires_at=expires_at
    )


def _build(db, context):
    built = build(
        db,
        context[0],
        context[1],
        selection=None,
        policy=policy(),
        profile=profile(),
        analysis_identity="analysis-1",
    )
    assert isinstance(built, PreparedContext)
    return built


def _analysis_pins(db):
    return db.scalars(select(EvidencePin).where(EvidencePin.kind == "analysis")).all()


def _targets(built, source_key=None):
    rows = [*built.required_targets, *built.optional_targets]
    if source_key is None:
        return rows
    return [row for row in rows if row.source_key == source_key]


def test_acquire_pins_every_contributing_row(db, context):
    _context(db, context)
    configmap = _configmaps(db, context)
    built = _build(db, context)
    assert built.optional_targets
    ids = {row.evidence_id for row in [*built.required_targets, *built.optional_targets]}
    assert configmap.id in ids
    analysis_id = uuid4()
    assert acquire(db, analysis_id, built) == "acquired"
    db.commit()
    pins = _analysis_pins(db)
    assert {pin.evidence_id for pin in pins} == ids
    assert len(pins) == len(ids)
    assert {pin.owner_id for pin in pins} == {analysis_id}
    from cloudops.features.investigations.storage import evidence_views

    views = evidence_views(db, list(db.scalars(select(Evidence))))
    assert all(view.content_status == "available" for view in views)


def test_second_acquire_is_idempotent(db, context):
    _context(db, context)
    built = _build(db, context)
    analysis_id = uuid4()
    assert acquire(db, analysis_id, built) == "acquired"
    db.commit()
    count = len(_analysis_pins(db))
    assert acquire(db, analysis_id, built) == "acquired"
    db.commit()
    assert len(_analysis_pins(db)) == count


def test_expired_optional_contributor_denies_whole_acquire(db, context):
    _context(db, context)
    configmap = _configmaps(db, context)
    built = _build(db, context)
    aliases = {row.alias for row in _targets(built, "kubernetes.configmap.metadata")}
    assert aliases
    configmap.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    denied = acquire(db, uuid4(), built)
    assert isinstance(denied, PinDenied)
    assert denied.reason == "contributing_content_unavailable"
    assert denied.slot == "kubernetes.configmap.metadata"
    assert denied.alias in aliases
    db.commit()
    assert _analysis_pins(db) == []
    db.expire_all()
    assert db.get(Evidence, configmap.id).payload is not None


def test_purge_before_pin_denies_without_resurrection(db, context):
    _context(db, context)
    configmap = _configmaps(db, context)
    built = _build(db, context)
    configmap.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    assert purge_expired(db, apply=True) == 1
    db.expire_all()
    assert db.get(Evidence, configmap.id).payload is None
    denied = acquire(db, uuid4(), built)
    assert isinstance(denied, PinDenied)
    db.commit()
    assert _analysis_pins(db) == []
    assert db.get(Evidence, configmap.id).payload is None


def test_pin_before_purge_keeps_content(db, context):
    state = _context(db, context, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    built = _build(db, context)
    assert acquire(db, uuid4(), built) == "acquired"
    db.commit()
    unrelated = add_evidence(
        db,
        context,
        "kubernetes.configmap.metadata",
        CONFIGMAPS,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    state_row = db.get(Evidence, state.id)
    state_row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    assert purge_expired(db, apply=True) == 1
    db.expire_all()
    kept = db.get(Evidence, state.id)
    assert kept.payload == STATE
    assert db.get(Evidence, unrelated.id).payload is None
    from cloudops.features.investigations.storage import evidence_views

    assert evidence_views(db, [kept])[0].content_status == "available"


def test_release_preserves_latest_and_resolution_owners(db, context):
    _context(db, context)
    built = _build(db, context)
    analysis_id = uuid4()
    assert acquire(db, analysis_id, built) == "acquired"
    db.add(
        EvidencePin(
            evidence_id=built.required_targets[0].evidence_id, kind="latest", owner_id=context[0]
        )
    )
    db.add(
        EvidencePin(
            evidence_id=built.required_targets[0].evidence_id,
            kind="resolution",
            owner_id=context[1],
        )
    )
    db.commit()
    pinned = len(_analysis_pins(db))
    assert pinned > 0
    assert release(db, analysis_id) == pinned
    db.commit()
    assert _analysis_pins(db) == []
    remaining = db.scalars(select(EvidencePin)).all()
    assert {(pin.kind, pin.owner_id) for pin in remaining} == {
        ("latest", context[0]),
        ("resolution", context[1]),
    }


def test_double_release_is_safe(db, context):
    _context(db, context)
    built = _build(db, context)
    analysis_id = uuid4()
    assert acquire(db, analysis_id, built) == "acquired"
    db.commit()
    assert release(db, analysis_id) > 0
    db.commit()
    assert release(db, analysis_id) == 0
    assert db.scalar(select(EvidencePin).where(EvidencePin.owner_id == analysis_id)) is None


def test_release_then_expiry_is_explicit(db, context):
    state = _context(db, context, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    built = _build(db, context)
    analysis_id = uuid4()
    assert acquire(db, analysis_id, built) == "acquired"
    db.commit()
    assert release(db, analysis_id) > 0
    db.commit()
    row = db.get(Evidence, state.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    from cloudops.features.investigations.storage import evidence_views

    assert evidence_views(db, [row])[0].content_status == "expired"
    with pytest.raises(DomainError) as error:
        evidence_content(db, context[0], context[1], state.id)
    assert error.value.status == 410
    assert error.value.code == "evidence_expired"


def test_analysis_pin_does_not_change_pack_coverage_or_run(db, context):
    _context(db, context)
    built = _build(db, context)
    run = db.get(InvestigationRun, context[1])
    before_run = (run.state, run.reason, list(run.pack_coverage))
    before_evidence = {
        row.id: (row.sha256, row.payload, dict(row.coverage), row.expires_at)
        for row in db.scalars(select(Evidence))
    }
    statements: list[str] = []
    engine = db.get_bind()

    def capture(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert acquire(db, uuid4(), built) == "acquired"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    db.commit()
    assert any(
        "FROM evidence" in sql and "FOR UPDATE" in sql and "ORDER BY evidence.id" in sql
        for sql in statements
    )
    db.expire_all()
    run = db.get(InvestigationRun, context[1])
    assert (run.state, run.reason, list(run.pack_coverage)) == before_run
    for row in db.scalars(select(Evidence)):
        assert (row.sha256, row.payload, dict(row.coverage), row.expires_at) == before_evidence[
            row.id
        ]
