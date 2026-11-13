from __future__ import annotations

import json
from pathlib import Path

import pytest
from diagnosis_fixtures import profile
from pydantic import ValidationError

from cloudops.features.diagnosis.egress import EgressPolicy, EgressPolicyError

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
