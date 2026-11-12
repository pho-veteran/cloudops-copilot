from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cloudops.features.diagnosis.profile import (
    ModelProfile,
    ProfileError,
    validate_base_url,
)
from diagnosis_fixtures import profile


def _invalid(**overrides):
    return pytest.raises(ValidationError)


def test_defaults_and_request_url():
    value = profile()
    assert value.request_url() == "http://127.0.0.1:18080/v1/chat/completions"
    assert value.identity().requested_model == "fixture-model"
    assert value.identity().config_id == "test-config"
    assert len(value.behavior_digest()) == 64


def test_disabled_profile_needs_no_endpoint():
    value = profile(
        mode="disabled",
        endpoint_label=None,
        base_url=None,
        model=None,
        output_limit=None,
        authorization_id=None,
        budget_policy_id=None,
    )
    assert value.mode.value == "disabled"


@pytest.mark.parametrize(
    "url,flag,expected",
    [
        ("https://model.example.com/v1", False, "https://model.example.com/v1/chat/completions"),
        (
            "https://model.example.com/api/v1/",
            False,
            "https://model.example.com/api/v1/chat/completions",
        ),
        ("https://model.example.com", False, "https://model.example.com/chat/completions"),
        (
            "https://model.example.com/v1beta",
            False,
            "https://model.example.com/v1beta/chat/completions",
        ),
        ("http://127.0.0.1:18080/v1", True, "http://127.0.0.1:18080/v1/chat/completions"),
        ("http://localhost/v1", True, "http://localhost/v1/chat/completions"),
        ("http://[::1]:9000/v1", True, "http://[::1]:9000/v1/chat/completions"),
        ("HTTPS://MODEL.EXAMPLE.COM/v1", False, "https://MODEL.EXAMPLE.COM/v1/chat/completions"),
    ],
)
def test_base_url_constructs_request_url(url, flag, expected):
    value = validate_base_url(url, flag)
    assert value + "/chat/completions" == expected


@pytest.mark.parametrize(
    "url,flag",
    [
        ("http://model.example.com/v1", True),
        ("http://127.0.0.1/v1", False),
        ("ftp://model.example.com/v1", False),
        ("https://user:pass@model.example.com/v1", False),
        ("https://model.example.com/v1?x=1", False),
        ("https://model.example.com/v1#x", False),
        ("https://model.example.com/v1/../chat", False),
        ("https://model.example.com/v1//chat", False),
        ("https://model.example.com/%2e%2e/chat", False),
        ("https://model.example.com/v1/chat/completions", False),
        ("https://model.example.com/v1/chat/completions/", False),
        ("https://model.example.com:0/v1", False),
        ("https://model.example.com:65536/v1", False),
        ("https://mødel.example.com/v1", False),
    ],
)
def test_base_url_rejections(url, flag):
    with pytest.raises(ValueError):
        validate_base_url(url, flag)


def test_profile_rejects_extra_fields_and_unknown_schema():
    with pytest.raises(ValidationError):
        profile(unknown=True)
    with pytest.raises(ValidationError):
        profile(**{"schema": "DiagnosisModelProfile/v2"})


def test_model_and_config_validation():
    with pytest.raises(ValidationError):
        profile(config_id="")
    with pytest.raises(ValidationError):
        profile(model="")
    with pytest.raises(ValidationError):
        profile(model="bad\nmodel")
    with pytest.raises(ValidationError):
        profile(endpoint_label="Bad Label")


def test_credential_reference_validation():
    with pytest.raises(ValidationError):
        profile(credential={"kind": "env", "name": "lowercase"})
    with pytest.raises(ValidationError):
        profile(credential={"kind": "file", "name": "X"})
    assert profile(credential=None).credential is None


def test_output_limit_choices():
    assert (
        profile(output_limit={"param": "max_tokens", "value": 1}).output_limit.param.value
        == "max_tokens"
    )
    assert (
        profile(
            output_limit={"param": "max_completion_tokens", "value": 384000}
        ).output_limit.param.value
        == "max_completion_tokens"
    )
    omitted = profile(
        output_limit={"param": "omit", "value": None, "omit_policy_id": "endpoint-policy"}
    )
    assert omitted.output_limit.value is None
    for value in (
        {"param": "max_tokens"},
        {"param": "max_tokens", "value": 0},
        {"param": "max_tokens", "value": 384001},
        {"param": "max_tokens", "value": 8, "omit_policy_id": "x"},
        {"param": "omit", "value": 8, "omit_policy_id": "x"},
        {"param": "omit", "value": None},
    ):
        with pytest.raises(ValidationError):
            profile(output_limit=value)


def test_limits_reject_bool_and_out_of_range():
    with pytest.raises(ValidationError):
        profile(limits={"max_input_bytes": True})
    with pytest.raises(ValidationError):
        profile(limits={"max_input_bytes": 0})
    with pytest.raises(ValidationError):
        profile(limits={"max_response_bytes": 262145})
    with pytest.raises(ValidationError):
        profile(limits={"deadline_seconds": 0.5})


def test_proxy_validation():
    assert profile(proxy={"url": "http://127.0.0.1:3128"}).proxy.url == "http://127.0.0.1:3128"
    for value in (
        {"url": "socks5://127.0.0.1:1"},
        {"url": "http://user:pass@127.0.0.1:1"},
        {"url": "http://127.0.0.1:1/path"},
        {"url": "http://127.0.0.1:1?x"},
    ):
        with pytest.raises(ValidationError):
            profile(proxy=value)


def test_digest_includes_behavior_and_excludes_activation_metadata():
    base = profile()
    assert base.behavior_digest() == profile(mode="operational").behavior_digest()
    assert (
        base.behavior_digest()
        == profile(endpoint_label="other", config_id="other").behavior_digest()
    )
    assert base.behavior_digest() != profile(model="other-model").behavior_digest()
    assert base.behavior_digest() != profile(authorization_id="other-owner").behavior_digest()
    assert (
        base.behavior_digest()
        != profile(output_limit={"param": "max_completion_tokens", "value": 32}).behavior_digest()
    )


def test_load_rejects_missing_and_invalid_files(tmp_path):
    with pytest.raises(ProfileError):
        ModelProfile.load(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError):
        ModelProfile.load(tmp_path / "bad.json")
    (tmp_path / "large.json").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ProfileError):
        ModelProfile.load(tmp_path / "large.json")


def test_load_optional_missing_and_valid(tmp_path):
    assert ModelProfile.load_optional(tmp_path / "missing.json") is None
    data = profile().model_dump(mode="json", by_alias=True)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = ModelProfile.load(path)
    assert loaded.request_url().endswith("/chat/completions")


def test_ca_file_is_resolved_relative_to_profile(tmp_path):
    pem = tmp_path / "ca.pem"
    pem.write_text("not-a-cert", encoding="utf-8")
    data = profile().model_dump(mode="json", by_alias=True)
    data["tls"] = {"ca_file": "ca.pem"}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProfileError):
        ModelProfile.load(path)


@pytest.mark.parametrize("override", [{"tls": {"ca_file": "\ud800"}}, {"model": "\udfff"}])
def test_profile_rejects_surrogates_before_use(override, tmp_path):
    with pytest.raises(ValidationError):
        profile(**override)
    data = profile().model_dump(mode="json", by_alias=True) | override
    path = tmp_path / "invalid-unicode.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProfileError):
        ModelProfile.load(path)


def test_committed_example_profile_is_valid():
    root = Path(__file__).resolve().parents[3]
    loaded = ModelProfile.load(root / "config/diagnosis-model.example.json")
    assert loaded.mode.value == "evaluation_only"
    assert loaded.request_url().endswith("/v1/chat/completions")
    assert loaded.credential.name == "DIAGNOSIS_MODEL_TOKEN"


def test_profile_is_frozen_after_validation():
    value = profile()
    with pytest.raises(ValidationError):
        value.base_url = "http://127.0.0.1:1/v1"


def test_load_rejects_recursive_profile_json(tmp_path):
    path = tmp_path / "recursive.json"
    path.write_bytes(b"[" * 30_000 + b"]" * 30_000)
    with pytest.raises(ProfileError):
        ModelProfile.load(path)
