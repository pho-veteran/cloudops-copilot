from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from diagnosis_fixtures import profile
from pydantic import ValidationError

from cloudops.features.diagnosis.context import (
    ContextScope,
    PreparedContext,
    SlotMaterial,
    assemble,
)
from cloudops.features.diagnosis.egress import (
    EgressPolicy,
    EgressPolicyError,
    check_send,
    prepare_probe,
    probe_expected_text,
)
from cloudops.features.diagnosis.messages import serialize_payload
from cloudops.features.diagnosis.versions import MAX_FEEDBACK_BYTES, SOURCE_CLASSES
from cloudops.features.investigations.locators import Entry, canonical
from cloudops.features.knowledge.guidance_schemas import KnowledgeRunScope

EXAMPLE = Path(__file__).resolve().parents[3] / "config/diagnosis-egress.example.json"
CLASSES = [
    "operator_request",
    "kubernetes_state",
    "kubernetes_events",
    "container_logs",
    "prometheus_series",
    "runbook",
    "synthetic_fixture",
    "diagnostic_fixture",
]


def policy(**overrides):
    value = {
        "schema": "EgressPolicy/v1",
        "policy_id": "test-policy",
        "policy_version": "1",
        "destination": {
            "config_id": "test-config",
            "config_version": "1",
            "endpoint_label": "loopback",
        },
        "allowed_source_classes": list(CLASSES),
        "max_message_bytes": 65536,
        "max_target_bytes": 8192,
        "redaction_policy_version": "redaction/v1",
        "deny_patterns": [
            {"name": "authorization_header", "regex": "(?i)\\bbearer\\s+[A-Za-z0-9._~+/-]{16,}"},
        ],
    }
    value.update(overrides)
    return EgressPolicy.model_validate(value)


def test_policy_denies_everything_by_default():
    loaded = EgressPolicy.load(EXAMPLE)
    assert loaded.policy_id == "example-deny-all"
    assert loaded.allowed_source_classes == []
    for source_class in CLASSES:
        assert loaded.allows(source_class) is False


def test_policy_rejects_unknown_fields_and_out_of_range_values():
    with pytest.raises(ValidationError):
        policy(extra_field=True)
    with pytest.raises(ValidationError):
        policy(max_message_bytes=65537)
    with pytest.raises(ValidationError):
        policy(max_message_bytes=0)
    with pytest.raises(ValidationError):
        policy(max_target_bytes=9000, max_message_bytes=8192)
    with pytest.raises(ValidationError):
        policy(max_target_bytes=255)
    with pytest.raises(ValidationError):
        policy(allowed_source_classes=["not_a_class"])
    with pytest.raises(ValidationError):
        policy(deny_patterns=[{"name": "p", "regex": "x"}] * 21)
    with pytest.raises(ValidationError):
        policy(deny_patterns=[{"name": "p", "regex": "x" * 513}])
    with pytest.raises(ValidationError):
        policy(destination={"config_id": "c", "config_version": "1", "endpoint_label": "Bad Label"})
    with pytest.raises(ValidationError):
        policy(
            destination={
                "config_id": "c",
                "config_version": "1",
                "endpoint_label": "x",
                "url": "https://example.test",
            }
        )


def test_invalid_regex_is_rejected_without_echoing_the_pattern(tmp_path):
    pattern = "(unclosed-secret-value"
    path = tmp_path / "invalid-regex.json"
    value = policy().model_dump(mode="json", by_alias=True)
    value["deny_patterns"] = [{"name": "broken", "regex": pattern}]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(EgressPolicyError) as caught:
        EgressPolicy.load(path)
    assert "unclosed-secret-value" not in str(caught.value)
    with pytest.raises(ValidationError):
        policy(deny_patterns=[{"name": "broken", "regex": pattern}])


def test_policy_digest_is_canonical_and_stable():
    first = policy()
    reordered = EgressPolicy.model_validate(
        json.loads(json.dumps(policy().model_dump(mode="json"), sort_keys=True))
    )
    assert first.digest() == reordered.digest()
    assert first.digest() != policy(policy_version="2").digest()
    assert (
        first.digest()
        != policy(
            destination={
                "config_id": "test-config",
                "config_version": "1",
                "endpoint_label": "other",
            }
        ).digest()
    )
    assert (
        first.digest() != policy(deny_patterns=[{"name": "other", "regex": "(?i)secret"}]).digest()
    )
    assert len(first.digest()) == 64


def test_destination_matches_exact_call_identity():
    assert policy().destination_matches(profile().identity()) is True
    assert (
        policy(
            destination={
                "config_id": "test-config",
                "config_version": "2",
                "endpoint_label": "loopback",
            }
        ).destination_matches(profile().identity())
        is False
    )
    assert (
        policy(
            destination={
                "config_id": "test-config",
                "config_version": "1",
                "endpoint_label": "remote",
            }
        ).destination_matches(profile().identity())
        is False
    )


def test_policy_scan_returns_only_the_pattern_name():
    loaded = policy(
        deny_patterns=[
            {"name": "assigned_secret", "regex": "(?i)\\b(password|token)\\s*[:=]\\s*\\S{8,}"}
        ]
    )
    matched = loaded.scan("password: hidden-value-123")
    assert matched == "assigned_secret"
    assert "hidden-value-123" not in str(matched)
    assert loaded.scan("nothing sensitive here") is None


def test_missing_file_is_no_policy(tmp_path):
    assert EgressPolicy.load_optional(None) is None
    assert EgressPolicy.load_optional(tmp_path / "absent.json") is None
    with pytest.raises(EgressPolicyError):
        EgressPolicy.load(tmp_path / "absent.json")


def test_load_rejects_oversized_or_invalid_json(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_text("{}" + " " * (64 * 1024), encoding="utf-8")
    with pytest.raises(EgressPolicyError):
        EgressPolicy.load(oversized)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    with pytest.raises(EgressPolicyError):
        EgressPolicy.load(invalid)


def test_example_config_validates():
    loaded = EgressPolicy.load(EXAMPLE)
    assert loaded.schema_ == "EgressPolicy/v1"
    assert loaded.max_message_bytes == 65536
    assert loaded.max_target_bytes == 8192
    assert loaded.redaction_policy_version == "redaction/v1"
    assert [pattern.name for pattern in loaded.deny_patterns] == [
        "authorization_header",
        "private_key_block",
        "assigned_secret",
    ]


CLOSED_CODES = {
    "policy_missing",
    "policy_changed",
    "profile_identity_changed",
    "destination_mismatch",
    "source_class_denied",
    "content_denied",
    "budget_exceeded",
    "message_shape_invalid",
    "repair_unavailable",
    "probe_not_allowed",
}


def _scope(request_text="Why did the workload restart?"):
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


def _material(slot, entries):
    return SlotMaterial(
        slot=slot,
        source_class=SOURCE_CLASSES[slot],
        evidence_id=uuid4(),
        evidence_sha256="a" * 64,
        payload_version=1,
        coverage_status="available",
        coverage_reason_codes=(),
        content_status="available",
        time_relation="within_requested_window",
        entries=tuple(entries),
        ambiguous_omitted=0,
    )


def _context(*, policy_value=None, request_text="Why did the workload restart?"):
    materials = [
        _material("kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1"})]),
        _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})]),
    ]
    built = assemble(
        materials,
        _scope(request_text),
        None,
        policy_value or policy(),
        profile(),
        "analysis-1",
    )
    assert isinstance(built, PreparedContext)
    return built


def test_check_send_rejects_policy_digest_change():
    changed = policy(allowed_source_classes=["operator_request", "kubernetes_events"])
    check = check_send(_context(), changed, profile(), "initial")
    assert check.ok is False
    assert check.code == "policy_changed"
    assert check.messages is None
    assert check.byte_size is None


def test_check_send_rejects_profile_identity_change():
    changed = profile(output_limit={"param": "max_completion_tokens", "value": 64})
    check = check_send(_context(), policy(), changed, "initial")
    assert check.ok is False
    assert check.code == "profile_identity_changed"
    assert check.messages is None


def test_check_send_rejects_destination_mismatch():
    remote = policy(
        destination={
            "config_id": "test-config",
            "config_version": "1",
            "endpoint_label": "remote",
        }
    )
    context = _context().model_copy(
        update={"policy_digest": remote.digest(), "policy_version": remote.policy_version}
    )
    check = check_send(context, remote, profile(), "initial")
    assert check.ok is False
    assert check.code == "destination_mismatch"
    assert check.messages is None


def test_check_send_rejects_missing_policy():
    check = check_send(_context(), None, profile(), "initial")
    assert check.ok is False
    assert check.code == "policy_missing"
    assert check.messages is None


def test_check_send_rejects_unallowlisted_class():
    denied = policy(allowed_source_classes=["operator_request", "kubernetes_events"])
    context = _context().model_copy(
        update={"policy_digest": denied.digest(), "policy_version": denied.policy_version}
    )
    check = check_send(context, denied, profile(), "initial")
    assert check.ok is False
    assert check.code == "source_class_denied"
    assert check.messages is None


def test_deny_pattern_in_request_text_fails_without_echo():
    strict = policy(
        deny_patterns=[{"name": "assigned_secret", "regex": "(?i)\\bpassword\\s*[:=]\\s*\\S{8,}"}]
    )
    context = _context().model_copy(
        update={
            "policy_digest": strict.digest(),
            "policy_version": strict.policy_version,
            "request": _context().request.model_copy(update={"text": "password=hunter2hunter2"}),
        }
    )
    check = check_send(context, strict, profile(), "initial")
    assert check.ok is False
    assert check.code == "content_denied"
    assert check.messages is None
    assert "hunter2" not in repr(check)
    assert "hunter2" not in str(check.code)


def test_deny_pattern_in_target_value_fails_without_echo():
    token = "Bearer " + "a" * 32
    context = _context()
    tampered = context.required_targets[0].model_copy(update={"value": {"uid": token}})
    context = context.model_copy(
        update={"required_targets": [tampered, *context.required_targets[1:]]}
    )
    check = check_send(context, policy(), profile(), "initial")
    assert check.ok is False
    assert check.code == "content_denied"
    assert check.messages is None
    assert token not in repr(check)


def test_initial_and_retry_use_identical_messages_and_are_both_rechecked():
    context = _context()
    initial = check_send(context, policy(), profile(), "initial")
    retry = check_send(context, policy(), profile(), "retry")
    assert initial.ok is True and retry.ok is True
    assert initial.messages == retry.messages
    assert initial.digest == retry.digest
    assert initial.byte_size == retry.byte_size
    changed = check_send(context, policy(policy_version="2"), profile(), "retry")
    assert changed.ok is False
    assert changed.code == "policy_changed"


def test_repair_appends_only_structural_codes_and_paths():
    context = _context()
    initial = check_send(context, policy(), profile(), "initial")
    feedback = (
        "VALIDATION_ERRORS:\n"
        "- supporting_evidence_ids[0]: unknown_alias\n"
        "- ruled_out_hypotheses[1]: ruled_out_partial_coverage"
    )
    repaired = check_send(context, policy(), profile(), "repair", feedback=feedback)
    assert repaired.ok is True
    assert repaired.messages[0] == initial.messages[0]
    assert repaired.messages[1].content.startswith(initial.messages[1].content)
    assert repaired.messages[1].content.endswith(feedback)
    assert repaired.byte_size == len(serialize_payload(profile(), repaired.messages))
    assert repaired.byte_size <= context.effective_budget_bytes


def test_repair_rejects_oversized_feedback_without_echo():
    context = _context()
    feedback = "VALIDATION_ERRORS:\n- x[0]: unknown_alias\n" + "y" * MAX_FEEDBACK_BYTES
    repaired = check_send(context, policy(), profile(), "repair", feedback=feedback)
    assert repaired.ok is False
    assert repaired.code in CLOSED_CODES
    assert repaired.messages is None
    assert "y" * 64 not in repr(repaired)


def test_repair_refused_when_no_headroom():
    wide = _context()
    tight_policy = policy(
        max_message_bytes=wide.message_bytes,
        max_target_bytes=min(8192, wide.message_bytes),
    )
    tight = _context(policy_value=tight_policy)
    assert tight.repair_available is False
    check = check_send(
        tight,
        tight_policy,
        profile(),
        "repair",
        feedback="VALIDATION_ERRORS:\n- x[0]: unknown_alias",
    )
    assert check.ok is False
    assert check.code == "repair_unavailable"
    assert check.messages is None


def test_probe_requires_diagnostic_fixture_and_matching_identity():
    assert prepare_probe(EgressPolicy.load(EXAMPLE), profile()).code == "probe_not_allowed"
    without_fixture = policy(
        allowed_source_classes=[name for name in CLASSES if name != "diagnostic_fixture"]
    )
    assert prepare_probe(without_fixture, profile()).code == "probe_not_allowed"
    assert (
        prepare_probe(
            policy(allowed_source_classes=["diagnostic_fixture"]), profile(endpoint_label="remote")
        ).code
        == "probe_not_allowed"
    )
    probe_policy = policy(allowed_source_classes=["diagnostic_fixture"])
    probe = prepare_probe(probe_policy, profile())
    assert probe.ok is True
    assert probe_expected_text() == "OK"
    assert [message.role for message in probe.messages] == ["system", "user"]
    assert "OK" in probe.messages[1].content
    assert probe.byte_size == len(serialize_payload(profile(), probe.messages))
    assert probe.byte_size <= probe_policy.max_message_bytes
    assert probe.digest


def test_sendcheck_code_contains_no_payload_text():
    context = _context()
    changed = policy(allowed_source_classes=["operator_request"])
    failures = [
        check_send(context, changed, profile(), "initial"),
        check_send(
            context,
            policy(),
            profile(output_limit={"param": "max_completion_tokens", "value": 64}),
            "initial",
        ),
        check_send(
            context.model_copy(update={"effective_budget_bytes": 1}), policy(), profile(), "initial"
        ),
        check_send(
            context,
            policy(),
            profile(),
            "repair",
            feedback="VALIDATION_ERRORS:\n- x[0]: unknown_alias\n" + "y" * 4096,
        ),
        prepare_probe(EgressPolicy.load(EXAMPLE), profile()),
    ]
    for check in failures:
        assert check.ok is False
        assert check.code in CLOSED_CODES
        assert check.messages is None
    assert all("Why did" not in repr(check) for check in failures)


def test_check_send_measures_with_serialize_payload():
    context = _context()
    check = check_send(context, policy(), profile(), "initial")
    assert check.ok is True
    assert check.byte_size == len(serialize_payload(profile(), check.messages))
    assert check.byte_size <= context.effective_budget_bytes
    tightened = context.model_copy(update={"effective_budget_bytes": context.message_bytes - 1})
    over = check_send(tightened, policy(), profile(), "initial")
    assert over.ok is False
    assert over.code == "budget_exceeded"
    assert over.messages is None


def test_check_send_rejects_a_non_r1_message_shape():
    context = _context().model_copy(update={"messages": [_context().messages[0]]})
    check = check_send(context, policy(), profile(), "initial")
    assert check.ok is False
    assert check.code == "message_shape_invalid"


FIXTURES = Path(__file__).resolve().parents[3] / "docs/evidence/e06-r2-context"


def _rebound(denied):
    return _context().model_copy(
        update={"policy_digest": denied.digest(), "policy_version": denied.policy_version}
    )


def test_egress_denial_matrix_matches_sendcheck_codes():
    matrix = json.loads((FIXTURES / "egress-denials.json").read_text(encoding="utf-8"))
    assert matrix["schema"] == "E06EgressDenialMatrix/v1"
    for row in matrix["cases"]:
        name = row["case"]
        expected = row["expected_code"]
        if name == "scan_deny_pattern":
            loaded = policy(deny_patterns=row["deny_patterns"])
            assert loaded.scan(row["text"]) == expected, name
            assert row["secret"] not in repr(loaded.scan(row["text"]))
            continue
        if name == "policy_missing":
            check = check_send(_context(), None, profile(), "initial")
        elif name == "policy_changed":
            check = check_send(
                _context(), policy(policy_version=row["policy_version"]), profile(), "initial"
            )
        elif name == "destination_mismatch":
            remote = policy(
                destination={
                    "config_id": "test-config",
                    "config_version": "1",
                    "endpoint_label": row["endpoint_label"],
                }
            )
            check = check_send(_rebound(remote), remote, profile(), "initial")
        elif name == "source_class_denied":
            denied = policy(allowed_source_classes=row["allowed"])
            check = check_send(_rebound(denied), denied, profile(), "initial")
        elif name == "content_denied":
            strict = policy(deny_patterns=row["deny_patterns"])
            context = _rebound(strict).model_copy(
                update={
                    "request": _context().request.model_copy(update={"text": row["request_text"]})
                }
            )
            check = check_send(context, strict, profile(), "initial")
            assert row["secret"] not in repr(check)
        elif name == "budget_exceeded":
            context = _context()
            assert context.message_bytes > row["bound"]
            check = check_send(
                context.model_copy(update={"effective_budget_bytes": row["bound"]}),
                policy(),
                profile(),
                "initial",
            )
        elif name == "ok":
            check = check_send(_context(), policy(), profile(), "initial")
            assert check.ok is True
            assert check.code is None
            assert check.byte_size == len(serialize_payload(profile(), check.messages))
            assert check.byte_size <= _context().effective_budget_bytes
            continue
        else:
            raise AssertionError(f"unhandled egress fixture case: {name}")
        assert check.ok is False, name
        assert check.code == expected, name
        assert check.code in CLOSED_CODES, name
        assert check.messages is None, name
        assert check.byte_size is None, name
        assert check.digest is None, name
