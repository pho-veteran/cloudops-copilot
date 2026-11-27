"""The selectable diagnosis context profile: identity, configuration and rollback."""

from __future__ import annotations

import json

from diagnosis_fixtures import profile

from cloudops.features.diagnosis import settings
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.engine import path, selection
from cloudops.features.diagnosis.versions import CONTEXT_VERSION, PROMPT_VERSION


def _policy() -> EgressPolicy:
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
            "allowed_source_classes": ["operator_request", "kubernetes_state"],
            "max_message_bytes": 65536,
            "max_target_bytes": 8192,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": [],
        }
    )


def _configure(tmp_path, monkeypatch, value=None):
    model = tmp_path / "model.json"
    model.write_text(json.dumps(profile().model_dump(mode="json", by_alias=True)))
    egress = tmp_path / "egress.json"
    egress.write_text(json.dumps(_policy().model_dump(mode="json", by_alias=True)))
    monkeypatch.setenv("DIAGNOSIS_PROFILE_FILE", str(model))
    monkeypatch.setenv("DIAGNOSIS_EGRESS_FILE", str(egress))
    monkeypatch.delenv("DIAGNOSIS_QUALIFICATION_FILE", raising=False)
    if value is None:
        monkeypatch.delenv("DIAGNOSIS_CONTEXT_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DIAGNOSIS_CONTEXT_PROFILE", value)
    return settings.load()


def test_default_profile_keeps_the_e06_identity(tmp_path, monkeypatch):
    deployment = _configure(tmp_path, monkeypatch)
    assert deployment.problem is None
    assert deployment.context_profile == "e06"
    identity = deployment.identity()
    assert identity["prompt_version"] == PROMPT_VERSION
    assert identity["context_version"] == CONTEXT_VERSION
    assert "context_profile" not in identity and "engine_version" not in identity
    assert _configure(tmp_path, monkeypatch, "e06").identity() == identity


def test_engine_profile_changes_the_identity_and_needs_new_qualification(tmp_path, monkeypatch):
    e06 = _configure(tmp_path, monkeypatch).identity()
    deployment = _configure(tmp_path, monkeypatch, "engine")
    identity = deployment.identity()
    assert deployment.context_profile == "engine"
    assert identity["prompt_version"] == path.PROMPT_VERSION
    assert identity["context_version"] == selection.CONTEXT_VERSION
    assert identity["context_profile"] == "engine"
    assert identity["engine_version"] == path.STAGES_VERSION
    assert identity != e06
    # A qualification of the E06 identity does not qualify the engine path.
    qualified = settings.Deployment(
        deployment.profile,
        deployment.policy,
        settings.Qualification(
            schema_version="DiagnosisQualification/v1",
            cohort_id="c",
            evidence_reference="r",
            identity=e06,
        ),
        context_profile="engine",
    )
    operational = qualified.profile.model_copy(update={"mode": "operational"})
    qualified = settings.Deployment(
        operational, qualified.policy, qualified.qualification, context_profile="engine"
    )
    assert qualified.denied() == "unqualified"


def test_unknown_profile_is_a_configuration_problem(tmp_path, monkeypatch):
    deployment = _configure(tmp_path, monkeypatch, "fastest")
    assert deployment.problem == "configuration_invalid"
    assert deployment.denied() == "configuration_invalid"


def test_refine_leaves_non_engine_contexts_unchanged():
    from types import SimpleNamespace

    advice = object()
    assert path.refine(advice, SimpleNamespace(context_version=CONTEXT_VERSION)) is advice
    assert "required_citations" in path.STAGES_VERSION
