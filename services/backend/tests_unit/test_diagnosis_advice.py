from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest
from diagnosis_fixtures import profile

from cloudops.features.diagnosis.advice import (
    RCAAdvice,
    RCAResult,
    ResolvedEvidence,
    ValidationCode,
    ValidationFailure,
    feedback_message,
    normalize,
    parse,
    validate_structural,
)
from cloudops.features.diagnosis.context import (
    ContextScope,
    PreparedContext,
    SlotMaterial,
    assemble,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.versions import (
    MAX_FEEDBACK_BYTES,
    MAX_FEEDBACK_FAILURES,
    SLOT_CAPS,
    SOURCE_CLASSES,
)
from cloudops.features.investigations.citation_schemas import CitationRef
from cloudops.features.investigations.locators import Entry, canonical
from cloudops.features.knowledge.guidance_schemas import KnowledgeRunScope

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


def _policy(*classes):
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


def _scope(request_text="Why is the workload in CrashLoopBackOff?"):
    now = datetime.now(timezone.utc)
    return ContextScope(
        knowledge=KnowledgeRunScope(
            incident_id=uuid4(),
            run_id=uuid4(),
            workload_id=uuid4(),
            cluster="cluster-a",
            namespace="namespace-a",
            name="app",
            workload_uid="uid-1",
            window_start=now - timedelta(minutes=30),
            window_end=now,
        ),
        request_text=request_text,
        request_source="operator",
        window_source="explicit",
    )


def _entry(kind, identity, value, label="Stored item"):
    return Entry(kind=kind, identity=canonical(identity), label=label, value=value)


def _material(slot, entries=(), *, coverage="available", content="available"):
    return SlotMaterial(
        slot=slot,
        source_class=SOURCE_CLASSES[slot],
        evidence_id=uuid4(),
        evidence_sha256="a" * 64,
        payload_version=1,
        coverage_status=coverage,
        coverage_reason_codes=(),
        content_status=content,
        time_relation="within_requested_window",
        entries=tuple(entries),
        ambiguous_omitted=0,
    )


def _missing(slot):
    return _material(slot, coverage="missing", content="missing")


def _materials(overrides):
    return [overrides.get(slot) or _missing(slot) for slot in SLOT_CAPS]


KUBERNETES = {
    "kubernetes.state": _material(
        "kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1"})]
    ),
    "kubernetes.events": _material(
        "kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})]
    ),
    "kubernetes.logs.current": _material(
        "kubernetes.logs.current",
        [_entry("log_segment", ["pod-1", "app", False, "1", "d"], {"lines": ["x"]})],
    ),
    "kubernetes.logs.previous": _material(
        "kubernetes.logs.previous",
        [_entry("log_segment", ["pod-1", "app", True, "1", "d"], {"lines": ["y"]})],
    ),
}

TRAFFIC = {
    "prometheus.request_rate": _material(
        "prometheus.request_rate",
        [_entry("series", ["metric", "request_rate"], {"metric": "request_rate"})],
    ),
    "prometheus.error_ratio": _material(
        "prometheus.error_ratio",
        [_entry("series", ["metric", "error_ratio"], {"metric": "error_ratio"})],
    ),
    "prometheus.p95_latency": _material(
        "prometheus.p95_latency",
        [_entry("series", ["metric", "p95_latency"], {"metric": "p95_latency"})],
    ),
}


def _context(overrides=None):
    materials = _materials({**KUBERNETES, **TRAFFIC, **(overrides or {})})
    built = assemble(
        materials,
        _scope(),
        None,
        _policy(),
        profile(),
        "analysis-1",
    )
    assert isinstance(built, PreparedContext)
    return built


def _traffic_context():
    materials = _materials(TRAFFIC)
    built = assemble(materials, _scope(), None, _policy(), profile(), "analysis-1")
    assert isinstance(built, PreparedContext)
    return built


def _resolver(context):
    targets = {
        target.alias: target for target in (*context.required_targets, *context.optional_targets)
    }

    def resolve(alias):
        target = targets[alias]
        return ResolvedEvidence(
            alias=alias,
            citation=CitationRef(
                run_id=context.run_id,
                evidence_id=target.evidence_id,
                locator=target.locator,
            ),
            source_key=target.source_key,
            source_class=target.source_class,
            evidence_sha256=target.evidence_sha256,
            entry_digest=target.entry_digest,
            time_relation=target.time_relation,
            coverage_status=target.coverage_status,
        )

    return resolve


def _advice(**overrides):
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
    return value


def _validated(**overrides):
    return RCAAdvice.model_validate(_advice(**overrides))


def _codes(failures):
    return [failure.code for failure in failures]


def test_supported_fixture_normalizes_in_advice_order():
    context = _context()
    advice = _validated(
        supporting_evidence_ids=["E002", "E001"],
        contradicting_evidence_ids=["E003"],
        alternatives=[
            {
                "hypothesis": "Image regression",
                "reason": "A new image tag rolled out.",
                "evidence_ids": ["E004"],
            }
        ],
        ruled_out_hypotheses=[
            {
                "family": "traffic_latency_errors",
                "reason": "No error-ratio signal.",
                "evidence_ids": ["E005", "E006", "E007"],
            }
        ],
        unverified_checks=[{"check": "Rollout history", "reason": "Not captured."}],
    )
    assert validate_structural(context, advice) == []
    result = normalize(context, advice, _resolver(context))
    assert isinstance(result, RCAResult)
    assert result.schema_version == "RCAResult/v1"
    assert result.run_id == context.run_id
    assert result.incident_id == context.incident_id
    assert result.context_digest == context.digest
    assert result.validator_version == "e06.validator/v1"
    assert result.prompt_version == "e06.rca_prompt/v1"
    assert result.context_version == "e06.context/v1"
    assert result.policy_version == context.policy_version
    assert result.status == "supported"
    assert result.machine_validation == "structural_v1"
    assert result.quality == "not_established"
    assert [item.alias for item in result.supporting] == ["E002", "E001"]
    assert [item.alias for item in result.contradicting] == ["E003"]
    assert [item.alias for item in result.alternatives[0].evidence] == ["E004"]
    assert [item.alias for item in result.ruled_out[0].evidence] == ["E005", "E006", "E007"]
    assert result.candidate_template_id == "restore_deployment_template"


def test_insufficient_fixture_normalizes_with_no_candidate():
    context = _context()
    advice = _validated(
        status="insufficient_evidence",
        cause_category="unknown",
        supporting_evidence_ids=[],
        candidate_template_id=None,
    )
    assert validate_structural(context, advice) == []
    result = normalize(context, advice, _resolver(context))
    assert result.status == "insufficient_evidence"
    assert result.cause_category == "unknown"
    assert result.candidate_template_id is None
    assert result.quality == "not_established"
    assert result.machine_validation == "structural_v1"


@pytest.mark.parametrize("field", ["scope", "model", "template_id", "parameters", "endpoint"])
def test_extra_fields_are_rejected(field):
    raw = json.dumps({**_advice(), field: "leak-marker-value"})
    failures = parse(raw)
    assert isinstance(failures, list)
    assert _codes(failures) == ["schema_invalid"]
    assert failures[0].path
    assert "leak-marker-value" not in failures[0].path
    assert "leak-marker-value" not in failures[0].model_dump_json()


def test_unknown_duplicate_and_wrong_class_aliases():
    context = _context()
    cases = [
        (_validated(supporting_evidence_ids=["E999"]), "unknown_alias"),
        (_validated(supporting_evidence_ids=["E001", "E001"]), "duplicate_alias"),
        (_validated(supporting_evidence_ids=["K001"]), "alias_wrong_class"),
        (_validated(runbook_citation_ids=["E001"]), "alias_wrong_class"),
        (_validated(runbook_citation_ids=["K999"]), "runbook_alias_unknown"),
    ]
    for advice, code in cases:
        codes = _codes(validate_structural(context, advice))
        assert code in codes, (code, codes)


def test_supported_requires_eligible_family_and_matching_family_evidence():
    traffic = _traffic_context()
    wrong_family = _validated(cause_category="crashloop_startup")
    assert "supported_requires_family_cause" in _codes(validate_structural(traffic, wrong_family))
    context = _context()
    other_family_alias = _validated(supporting_evidence_ids=["E005"])
    assert "supported_requires_family_evidence" in _codes(
        validate_structural(context, other_family_alias)
    )


def test_insufficient_requires_unknown_cause_and_null_candidate():
    context = _context()
    unknown_cause = _validated(cause_category="unknown")
    assert "supported_requires_family_cause" in _codes(validate_structural(context, unknown_cause))
    not_unknown = _validated(
        status="insufficient_evidence",
        supporting_evidence_ids=[],
        candidate_template_id=None,
    )
    assert "insufficient_requires_unknown_cause" in _codes(
        validate_structural(context, not_unknown)
    )
    candidate = _validated(
        status="insufficient_evidence",
        cause_category="unknown",
        supporting_evidence_ids=[],
        candidate_template_id="restore_deployment_template",
    )
    assert "candidate_not_allowed" in _codes(validate_structural(context, candidate))


@pytest.mark.parametrize(
    ("cause", "alias", "candidate"),
    [
        ("crashloop_startup", "E001", "restore_deployment_template"),
        ("oom_restarts", "E001", "set_memory_profile"),
        ("traffic_latency_errors", "E005", "scale_deployment"),
    ],
)
def test_candidate_matrix(cause, alias, candidate):
    context = _context()
    valid = _validated(
        cause_category=cause,
        supporting_evidence_ids=[alias],
        candidate_template_id=candidate,
    )
    assert validate_structural(context, valid) == []
    wrong = _validated(
        cause_category=cause,
        supporting_evidence_ids=[alias],
        candidate_template_id="scale_deployment"
        if candidate != "scale_deployment"
        else "restore_deployment_template",
    )
    assert "candidate_mismatch" in _codes(validate_structural(context, wrong))
    for cause_family in ("pending_unschedulable", "image_pull"):
        forbidden = _validated(
            cause_category=cause_family,
            supporting_evidence_ids=["E001"],
            candidate_template_id="restore_deployment_template",
        )
        assert "candidate_not_allowed" in _codes(validate_structural(context, forbidden))
    no_candidate = _validated(
        cause_category=cause, supporting_evidence_ids=[alias], candidate_template_id=None
    )
    assert validate_structural(context, no_candidate) == []


def test_ruled_out_requires_family_coverage():
    context = _context()
    incomplete = _validated(
        ruled_out_hypotheses=[
            {
                "family": "crashloop_startup",
                "reason": "Logs show an application exit.",
                "evidence_ids": ["E001", "E002", "E003"],
            }
        ]
    )
    assert "ruled_out_coverage_incomplete" in _codes(validate_structural(context, incomplete))
    outside = _validated(
        ruled_out_hypotheses=[
            {
                "family": "crashloop_startup",
                "reason": "Logs show an application exit.",
                "evidence_ids": ["E001", "E002", "E003", "E004", "E005"],
            }
        ]
    )
    assert "ruled_out_evidence_outside_family" in _codes(validate_structural(context, outside))
    unknown_family = _validated(
        ruled_out_hypotheses=[
            {
                "family": "cache_miss",
                "reason": "Not a known family.",
                "evidence_ids": ["E001"],
            }
        ]
    )
    assert "ruled_out_family_unknown" in _codes(validate_structural(context, unknown_family))
    failures = parse(
        json.dumps(
            _advice(
                ruled_out_hypotheses=[
                    {"family": "crashloop_startup", "reason": "x", "evidence_ids": []}
                ]
            )
        )
    )
    assert isinstance(failures, list)
    assert _codes(failures) == ["schema_invalid"]


def test_partial_coverage_blocks_ruled_out_but_not_supported():
    context = _context(
        {
            "kubernetes.state": _material(
                "kubernetes.state", KUBERNETES["kubernetes.state"].entries, coverage="partial"
            )
        }
    )
    ruled_out = _validated(
        ruled_out_hypotheses=[
            {
                "family": "crashloop_startup",
                "reason": "Logs show an application exit.",
                "evidence_ids": ["E001", "E002", "E003", "E004"],
            }
        ]
    )
    assert "ruled_out_partial_coverage" in _codes(validate_structural(context, ruled_out))
    supported = _validated(supporting_evidence_ids=["E001"])
    assert validate_structural(context, supported) == []


def test_waived_log_slot_does_not_block_ruled_out():
    waived = _material("kubernetes.logs.previous", coverage="not_applicable", content="missing")
    context = _context({"kubernetes.logs.previous": waived})
    advice = _validated(
        ruled_out_hypotheses=[
            {
                "family": "crashloop_startup",
                "reason": "Logs show an application exit.",
                "evidence_ids": ["E001", "E002", "E003"],
            }
        ]
    )
    assert validate_structural(context, advice) == []
    result = normalize(context, advice, _resolver(context))
    assert "ruled_out_slot_waived:kubernetes.logs.previous" in result.limitations


def test_runbook_only_support_is_rejected():
    context = _context()
    only_runbook = _validated(supporting_evidence_ids=[], runbook_citation_ids=["K001"])
    assert "runbook_only_support" in _codes(validate_structural(context, only_runbook))
    empty = _validated(supporting_evidence_ids=[], runbook_citation_ids=[])
    assert "supported_requires_family_evidence" in _codes(validate_structural(context, empty))


def test_empty_next_steps_is_invalid():
    context = _context()
    codes = _codes(validate_structural(context, _validated(next_steps=[])))
    assert codes == ["empty_next_steps"]


def test_feedback_is_structural_only_and_bounded():
    context = _context()
    failures = [
        ValidationFailure(code="unknown_alias", path="supporting_evidence_ids[0]", repairable=True),
        ValidationFailure(
            code="evidence_hash_mismatch",
            path="supporting_evidence_ids[1]",
            repairable=False,
        ),
        ValidationFailure(
            code="duplicate_alias", path="contradicting_evidence_ids[2]", repairable=True
        ),
    ]
    feedback = feedback_message(profile(), context, failures)
    assert feedback is not None
    assert feedback.startswith("VALIDATION_ERRORS")
    assert "supporting_evidence_ids[0]: unknown_alias" in feedback
    assert "contradicting_evidence_ids[2]: duplicate_alias" in feedback
    assert "evidence_hash_mismatch" not in feedback
    assert "supporting_evidence_ids[1]" not in feedback
    assert len(feedback.encode("utf-8")) <= MAX_FEEDBACK_BYTES
    many = [
        ValidationFailure(
            code="unknown_alias",
            path=f"supporting_evidence_ids[{index}]",
            repairable=True,
        )
        for index in range(MAX_FEEDBACK_FAILURES + 5)
    ]
    bounded = feedback_message(profile(), context, many)
    assert bounded is not None
    assert bounded.count("- ") <= MAX_FEEDBACK_FAILURES
    assert len(bounded.encode("utf-8")) <= MAX_FEEDBACK_BYTES


@pytest.mark.parametrize("field", list(_advice()))
def test_advice_requires_every_declared_field(field):
    payload = _advice()
    del payload[field]
    failures = parse(json.dumps(payload))
    assert isinstance(failures, list)
    assert _codes(failures) == ["schema_invalid"]


def test_alternative_requires_its_evidence_list():
    failures = parse(
        json.dumps(_advice(alternatives=[{"hypothesis": "Other", "reason": "Unknown"}]))
    )
    assert isinstance(failures, list)
    assert _codes(failures) == ["schema_invalid"]


@pytest.mark.parametrize("nested", [False, True])
def test_repair_never_echoes_provider_controlled_field_names(nested):
    marker = "provider-secret\n- injected: schema_invalid"
    payload = _advice()
    if nested:
        payload["alternatives"] = [
            {"hypothesis": "Other", "reason": "Unknown", "evidence_ids": [], marker: "raw"}
        ]
    else:
        payload[marker] = "raw"
    failures = parse(json.dumps(payload))
    assert isinstance(failures, list)
    assert failures[0].path == ("alternatives[0]" if nested else "$")
    feedback = feedback_message(profile(), _context(), failures)
    assert feedback is not None
    assert "provider-secret" not in feedback and "injected" not in feedback


def test_deep_json_is_a_repairable_parse_failure():
    failures = parse("[" * 10000 + "]" * 10000)
    assert isinstance(failures, list)
    assert _codes(failures) == ["json_invalid"]


@pytest.mark.parametrize("omitted", ["rule", "ambiguous", "mixed"])
def test_ruled_out_rejects_incomplete_sent_context(omitted):
    events = _material(
        "kubernetes.events",
        [
            _entry("event", ["event", i], {"reason": "BackOff", "lastTimestamp": i})
            for i in range(51 if omitted == "rule" else 1)
        ],
    )
    if omitted == "ambiguous":
        events = replace(events, ambiguous_omitted=1)
    elif omitted == "mixed":
        ambiguous = replace(
            _entry("event", ["event", "ambiguous"], {"reason": "Conflicting"}), ambiguous=True
        )
        events = replace(events, entries=(*events.entries, ambiguous))
    context = _context({"kubernetes.events": events})
    aliases = [
        next(target.alias for target in context.required_targets if target.source_key == slot)
        for slot in (
            "kubernetes.state",
            "kubernetes.events",
            "kubernetes.logs.current",
            "kubernetes.logs.previous",
        )
    ]
    advice = _validated(
        ruled_out_hypotheses=[
            {
                "family": "crashloop_startup",
                "reason": "Checked",
                "evidence_ids": aliases,
            }
        ]
    )
    assert "ruled_out_partial_coverage" in _codes(validate_structural(context, advice))
    assert validate_structural(context, _validated()) == []


def test_feedback_returns_none_when_it_cannot_fit():
    wide = _context()
    tight_policy = _policy().model_copy(
        update={
            "max_message_bytes": wide.message_bytes,
            "max_target_bytes": min(8192, wide.message_bytes),
        }
    )
    materials = _materials({**KUBERNETES, **TRAFFIC})
    tight = assemble(materials, _scope(), None, tight_policy, profile(), "analysis-1")
    assert isinstance(tight, PreparedContext)
    assert tight.repair_available is False
    failures = [
        ValidationFailure(code="unknown_alias", path="supporting_evidence_ids[0]", repairable=True)
    ]
    assert feedback_message(profile(), tight, failures) is None


FIXTURES = Path(__file__).resolve().parents[3] / "docs/evidence/e06-r2-context"


def _fixture_context():
    data = json.loads((FIXTURES / "prepared-context.json").read_text(encoding="utf-8"))
    return PreparedContext.model_validate(data)


def _fixture_advice(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["schema"] == "RCAAdvice/v1"
    parsed = parse(json.dumps(data["advice"]))
    assert isinstance(parsed, RCAAdvice)
    return parsed


def test_advice_fixtures_normalize_against_the_prepared_context():
    context = _fixture_context()
    supported = _fixture_advice("advice-supported.json")
    assert validate_structural(context, supported) == []
    result = normalize(context, supported, _resolver(context))
    assert [item.alias for item in result.supporting] == ["E002", "E001"]
    assert result.candidate_template_id == "restore_deployment_template"
    assert result.machine_validation == "structural_v1"
    assert result.quality == "not_established"
    insufficient = _fixture_advice("advice-insufficient.json")
    assert validate_structural(context, insufficient) == []
    other = normalize(context, insufficient, _resolver(context))
    assert other.status == "insufficient_evidence"
    assert other.cause_category == "unknown"
    assert other.candidate_template_id is None
    assert other.quality == "not_established"


def test_invalid_case_matrix_matches_the_validator_codes():
    matrix = json.loads((FIXTURES / "invalid-cases.json").read_text(encoding="utf-8"))
    assert matrix["schema"] == "E06InvalidCaseMatrix/v1"
    context = _fixture_context()
    closed = set(get_args(ValidationCode))
    for row in matrix["cases"]:
        parsed = parse(json.dumps(row["advice"]))
        assert isinstance(parsed, RCAAdvice), row["name"]
        codes = _codes(validate_structural(context, parsed))
        assert codes == row["expected_codes"], row["name"]
        assert set(row["expected_codes"]) <= closed, row["name"]
