"""Identity boundaries for the owned E04 fixture mutation."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("e04_probe", scripts / "probe-e04-integration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_probe_rejects_same_name_replacement_before_mutation(probe, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({"metadata": {"uid": "replacement"}}))

    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="UID changed; no mutation sent"):
        probe.change_revision({"name": "checkout", "namespace": "demo", "uid": "original"})
    assert len(calls) == 1
    assert "get" in calls[0] and "patch" not in calls[0]


def test_revision_probe_atomically_guards_uid_and_version_and_preserves_env(probe, monkeypatch):
    calls = []
    before = {
        "metadata": {"uid": "original", "resourceVersion": "12"},
        "spec": {
            "template": {"spec": {"containers": [{"env": [{"name": "KEEP", "value": "yes"}]}]}}
        },
    }
    after = {"metadata": {"uid": "original", "resourceVersion": "14"}}
    responses = iter([json.dumps(before), "", "", json.dumps(after)])

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(probe.subprocess, "run", run)
    result = probe.change_revision({"name": "checkout", "namespace": "demo", "uid": "original"})
    patch = json.loads(calls[1][-1])
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "original"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
    ]
    assert patch[2]["path"] == "/spec/template/spec/containers/0/env"
    assert patch[2]["value"][0] == {"name": "KEEP", "value": "yes"}
    assert patch[2]["value"][1]["name"] == "CLOUDOPS_E04_REVISION"
    assert result["uid_before"] == result["uid_after"] == "original"
