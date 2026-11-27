"""Engine-lab dataset v2 mechanisms: bounded template changes and a verified reset."""

import copy
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def driver(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "engine_lab_driver_test", ROOT / "scripts/manage-local-lab.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    monkeypatch.setattr(value, "DIRECTORY", tmp_path / "lab")
    monkeypatch.setattr(value, "OWNED_PROJECT", "cloudops-engine-lab")
    return value


@pytest.fixture
def lab(driver, monkeypatch):
    value = driver.Lab(driver.DIRECTORY)
    value.directory.mkdir(mode=0o700)
    (value.directory / "receipts").mkdir(mode=0o700)
    value.marker = {
        "project": "cloudops-engine-lab",
        "root": str(ROOT),
        "owner": str(uuid4()),
        "deployments": {"checkout": "original", "history": "history-uid"},
    }
    value.save()
    monkeypatch.setattr(value, "no_execution", lambda: None)
    monkeypatch.setattr(value, "wait_settled", lambda: None)
    monkeypatch.setattr(value, "wait", lambda check, description, seconds=180: check())
    return value


def canonical(driver):
    return {
        "metadata": {"name": "checkout", "namespace": "demo", "uid": "original"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 30,
                    "containers": [
                        {
                            "name": "app",
                            "image": driver.SAMPLE_CTR_IMAGE,
                            "imagePullPolicy": "Never",
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "400m", "memory": "160Mi"},
                            },
                            "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                        }
                    ],
                }
            },
        },
        "status": {"readyReplicas": 1},
    }


class FakeCluster:
    """A Deployment and an optional ResourceQuota, mutated only through the lab driver."""

    def __init__(self, deployment):
        self.deployment = deployment
        self.quota = None
        self.patches = []

    def get(self, kind, name, namespace=None):
        assert kind == "resourcequota"
        return self.quota

    def kube(self, *args, body=None, timeout=60):
        if args[:2] == ("get", "pods"):
            phases = ("Running", "Running", "Succeeded")
            return json.dumps({"items": [{"status": {"phase": phase}} for phase in phases]})
        if args[:2] == ("apply", "-f"):
            assert body["kind"] == "ResourceQuota" and body["spec"] == {"hard": {"pods": "2"}}
            self.quota = body
        elif args[:2] == ("delete", "resourcequota"):
            self.quota = None
        else:
            raise AssertionError(args)
        return ""

    def patch(self, resource, changes):
        self.patches.append(changes)
        for change in changes:
            if change["path"] == "/spec/template/spec":
                self.deployment["spec"]["template"]["spec"] = copy.deepcopy(change["value"])
            elif change["path"] == "/spec/replicas":
                self.deployment["spec"]["replicas"] = change["value"]


def wire(lab, monkeypatch, cluster):
    monkeypatch.setattr(lab, "deployment", lambda name: copy.deepcopy(cluster.deployment))
    monkeypatch.setattr(lab, "get", cluster.get)
    monkeypatch.setattr(lab, "kube", cluster.kube)
    monkeypatch.setattr(lab, "patch", cluster.patch)


@pytest.mark.parametrize("mechanism", ["profile", "crash-startup", "oom-alloc", "once", "quota"])
def test_every_mechanism_leaves_its_input_unchanged_and_breaks_the_baseline(driver, mechanism):
    deployment = canonical(driver)
    original = copy.deepcopy(deployment)
    spec, replicas, quota = driver.engine_template(
        mechanism, deployment["spec"]["template"]["spec"], 1, {"seed": "s", "kind": "oom"}
    )
    assert deployment == original
    changed = copy.deepcopy(deployment)
    changed["spec"]["template"]["spec"] = spec
    changed["spec"]["replicas"] = replicas
    assert driver.engine_baseline_problems(changed)
    assert quota == (mechanism == "quota")


def test_mechanisms_set_the_documented_cues(driver):
    base = canonical(driver)["spec"]["template"]["spec"]

    def run(mechanism, **params):
        return driver.engine_template(mechanism, base, 1, params)

    spec, _, _ = run("liveness", ignore_term="1")
    container = spec["containers"][0]
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert {"name": "LAB_IGNORE_TERM", "value": "1"} in container["env"]
    assert spec["terminationGracePeriodSeconds"] == 3
    spec, _, _ = run("image-invalid")
    assert spec["containers"][0]["image"] == "shop/Checkout:2.4.1"
    spec, _, _ = run("config-ref")
    assert spec["containers"][0]["env"][0]["valueFrom"]["configMapKeyRef"]["name"] == (
        "checkout-settings"
    )
    _, replicas, _ = run("scale-zero")
    assert replicas == 0
    spec, _, _ = run("pending-selector")
    assert spec["nodeSelector"] == {"node.kubernetes.io/pool": "checkout-large"}
    names = json.dumps([run(name)[0] for name in driver.ENGINE_MECHANISMS if name != "scale-zero"])
    for word in ("fault", "inject", "noise", "unavailable"):
        assert word not in names.lower()
    with pytest.raises(RuntimeError):
        run("profile", seed="bad value; rm")
    with pytest.raises(RuntimeError):
        driver.engine_template("delete-namespace", base, 1, {})


def test_apply_and_reset_restore_the_exact_baseline_and_remove_the_quota(lab, driver, monkeypatch):
    cluster = FakeCluster(canonical(driver))
    wire(lab, monkeypatch, cluster)
    before = copy.deepcopy(cluster.deployment)
    lab.engine_apply("profile", {"seed": "t1", "log_profile": "json"})
    lab.engine_apply("quota", {"seed": "t1"})
    lab.engine_apply("scale-zero", {})
    record = json.loads((lab.directory / "fault.json").read_text())
    assert [step["mechanism"] for step in record["steps"]] == ["profile", "quota", "scale-zero"]
    assert cluster.quota is not None and cluster.deployment["spec"]["replicas"] == 0
    result = lab.engine_reset()
    assert result["quota_removed"] and cluster.quota is None
    assert cluster.deployment["spec"] == before["spec"]
    assert not (lab.directory / "fault.json").exists()
    assert list((lab.directory / "receipts").glob("*reset-private.json"))


def test_apply_refuses_a_target_that_is_not_at_the_baseline(lab, driver, monkeypatch):
    deployment = canonical(driver)
    deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "LAB_MODE", "value": "slow"}
    ]
    cluster = FakeCluster(deployment)
    wire(lab, monkeypatch, cluster)
    with pytest.raises(RuntimeError, match="baseline: env"):
        lab.engine_apply("profile", {"seed": "t1"})
    assert not cluster.patches and not (lab.directory / "fault.json").exists()


def test_engine_mechanisms_are_refused_outside_the_engine_lab(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "OWNED_PROJECT", "cloudops-e10-s3-lab")
    with pytest.raises(RuntimeError, match="only in the engine lab"):
        lab.engine_apply("profile", {})


def test_fixed_reset_refuses_an_engine_record(lab, driver, monkeypatch):
    (lab.directory / "fault.json").write_text(json.dumps({"kind": "engine-v2", "uid": "original"}))
    monkeypatch.setattr(lab, "eligible_action_count", lambda: 0)
    monkeypatch.setattr(lab, "active_loads", lambda: [])
    with pytest.raises(RuntimeError, match="engine-reset"):
        lab.reset()
