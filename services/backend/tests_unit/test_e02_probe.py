"""Mutation guards for the disposable lab probe; no cluster or database is contacted."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def probe(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "e02_probe_guard_test", scripts / "probe-e02-integration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resource(kind="Deployment", owner="e02-baselines"):
    return {
        "apiVersion": "apps/v1" if kind == "Deployment" else "v1",
        "kind": kind,
        "metadata": {
            "namespace": "demo",
            "name": "e02-ready" if kind == "Deployment" else "e02-settings",
            "labels": {"cloudops-probe": owner},
            "uid": "fixture-uid",
            "resourceVersion": "opaque-version",
        },
        "spec": {"replicas": 1},
    }


@pytest.mark.parametrize("kind", ["Deployment", "ConfigMap"])
def test_existing_foreign_fixture_is_never_overwritten(probe, monkeypatch, kind):
    calls = []

    def kube(*args, body=None):
        calls.append(args)
        return json.dumps(resource(kind, owner="another-owner"))

    monkeypatch.setattr(probe, "kube", kube)
    with pytest.raises(RuntimeError, match="ownership"):
        probe.ensure_fixture(resource(kind))
    assert len(calls) == 1 and calls[0][0] == "get"


def test_get_failure_does_not_become_permission_to_create(probe, monkeypatch):
    calls = []

    def kube(*args, body=None):
        calls.append(args)
        raise subprocess.CalledProcessError(1, ["kubectl", "get"])

    monkeypatch.setattr(probe, "kube", kube)
    with pytest.raises(subprocess.CalledProcessError):
        probe.ensure_fixture(resource())
    assert len(calls) == 1 and calls[0][0] == "get"


def test_missing_fixture_uses_atomic_create_not_apply(probe, monkeypatch):
    calls = []

    def kube(*args, body=None):
        calls.append((args, body))
        if args[0] == "get":
            return ""
        # A concurrent creator wins the name. The probe must fail, not overwrite it.
        raise subprocess.CalledProcessError(1, ["kubectl", "create"])

    monkeypatch.setattr(probe, "kube", kube)
    with pytest.raises(subprocess.CalledProcessError):
        probe.ensure_fixture(resource())
    assert [args[0] for args, _ in calls] == ["get", "create"]


def test_fixture_update_is_uid_and_version_conditional(probe, monkeypatch):
    calls = []

    def kube(*args, body=None):
        calls.append((args, body))
        return json.dumps(resource()) if args[0] == "get" else "updated"

    monkeypatch.setattr(probe, "kube", kube)
    proposed = resource()
    proposed["metadata"].pop("uid")
    proposed["metadata"].pop("resourceVersion")
    probe.apply_fixture(proposed)
    assert calls[-1][1]["metadata"]["uid"] == "fixture-uid"
    assert calls[-1][1]["metadata"]["resourceVersion"] == "opaque-version"
    assert "uid" not in proposed["metadata"]


def test_fixture_delete_carries_preconditions_before_wait(probe, monkeypatch):
    calls = []

    def kube(*args, body=None):
        calls.append((args, body))
        return json.dumps(resource()) if args[0] == "get" else "removed"

    monkeypatch.setattr(probe, "kube", kube)
    probe.delete_fixture("Deployment", "e02-ready")
    assert [args[0] for args, _ in calls] == ["get", "delete", "wait"]
    assert calls[1][1]["preconditions"] == {
        "uid": "fixture-uid",
        "resourceVersion": "opaque-version",
    }
    assert calls[1][0][2] == "/apis/apps/v1/namespaces/demo/deployments/e02-ready"


def test_protected_checkout_is_rejected_before_any_mutation(probe, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected cluster contact")

    monkeypatch.setattr(probe, "kube", unexpected)
    with pytest.raises(RuntimeError, match="fixed disposable"):
        probe.delete_fixture("Deployment", "checkout")


def test_existing_owned_configmap_keeps_its_contents(probe, monkeypatch):
    calls = []

    def kube(*args, body=None):
        calls.append(args)
        return json.dumps(resource("ConfigMap"))

    monkeypatch.setattr(probe, "kube", kube)
    probe.ensure_fixture(resource("ConfigMap"))
    assert len(calls) == 1 and calls[0][0] == "get"


@pytest.mark.parametrize(
    "code", ["observation_busy", "observation_superseded", "baseline_unavailable"]
)
def test_negative_case_cannot_pass_on_an_unrelated_409(probe, code):
    import httpx

    with pytest.raises(RuntimeError, match="candidate_changed"):
        probe.error_response(
            httpx.Response(409, json={"error": {"code": code}}), 409, "candidate_changed"
        )


def test_source_hash_normalizes_only_line_endings(probe):
    assert probe.lf_hash(b"first\r\n second\r\n") == probe.lf_hash(b"first\n second\n")
    assert probe.lf_hash(b"first\n second\n") != probe.lf_hash(b"first\nsecond\n")


def test_optimized_python_cannot_disable_ownership_guard(probe):
    code = f"""import importlib.util,sys
sys.path.insert(0,{str(Path(probe.__file__).parent)!r})
spec=importlib.util.spec_from_file_location('optimized_probe',{probe.__file__!r})
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.kube=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Unexpected cluster contact'))
try: module.delete_fixture('Deployment','checkout')
except RuntimeError as error:
    if 'fixed disposable' not in str(error): raise
else: raise RuntimeError('Ownership guard was skipped')
"""
    result = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
