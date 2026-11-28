"""E10 S3 recovery-lab guards and fault mapping, without Docker, Kubernetes or PostgreSQL."""

import copy
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not (ROOT / "scripts/manage-local-lab.py").is_file(),
    reason="The owned lab driver requires POSIX locks and a source checkout.",
)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "recovery_lab_test", ROOT / "scripts/manage-local-lab.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    monkeypatch.setattr(value, "DIRECTORY", tmp_path / "lab")
    return value


@pytest.fixture
def lab(driver, monkeypatch):
    value = driver.Lab(driver.DIRECTORY)
    value.directory.mkdir(mode=0o700)
    value.marker = {
        "project": driver.OWNED_PROJECT,
        "root": str(ROOT),
        "owner": str(uuid4()),
        "deployments": {"checkout": "original", "history": "history-uid"},
    }
    value.save()
    for name, result in {
        "resources": {"container": [], "volume": [], "network": []},
        "eligible_action_count": 0,
        "no_execution": None,
        "receipt": None,
    }.items():
        monkeypatch.setattr(value, name, lambda *args, result=result, **kwargs: result)
    return value


def healthy(driver):
    return {
        "metadata": {"name": "checkout", "namespace": "demo", "uid": "original"},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "docker.io/library/" + driver.RECOVERY_IMAGES["healthy"],
                            "imagePullPolicy": "Never",
                            "resources": copy.deepcopy(driver.RECOVERY_RESOURCES),
                        }
                    ]
                }
            },
        },
    }


def ready_trial(driver, scenario_id):
    deployment = healthy(driver)
    return {
        "trial_id": str(uuid4()),
        "run_id": str(uuid4()),
        "window_id": str(uuid4()),
        "scenario_id": scenario_id,
        "scenario_kind": "recovery",
        "status": "ready",
        "catalog_sha256": driver.sha256_file(ROOT / "config/lab/release-scenarios-v1.json"),
        "target_uid": "original",
        "image_digests": ["sha256:" + "a" * 64],
        "source_binding_sha256": "b" * 64,
        "baseline": {"owned_fields": driver.owned_fields(deployment)},
        "load": {
            "state": "running",
            "container_id": "load-1",
            "rate": 8,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
        "qualification": {"rate_rps": 8},
        "phases": {"inject": None},
    }


def test_recovery_trials_require_the_recovery_lab(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", False)
    with pytest.raises(RuntimeError, match="E10 S3 recovery lab"):
        lab.begin_trial("recovery-oom-memory")
    assert not lab.trial_path.exists()


def test_recovery_faults_require_a_recovery_trial(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    monkeypatch.setattr(lab, "containment", lambda **kwargs: {})
    monkeypatch.setattr(lab, "deployment", lambda name: healthy(driver))
    monkeypatch.setattr(lab, "patch", lambda *args: pytest.fail("Unexpected write"))
    with pytest.raises(RuntimeError, match="ready recovery trial"):
        lab.fault("memory-limit")
    assert not (lab.directory / "fault.json").exists()


@pytest.mark.parametrize(
    ("scenario_id", "operation"),
    [
        ("recovery-oom-memory", "memory-limit"),
        ("recovery-bad-rollout-restore", "bad-rollout"),
        ("recovery-traffic-scale", "overload"),
    ],
)
def test_recovery_faults_change_only_the_field_the_action_repairs(
    lab, driver, monkeypatch, scenario_id, operation
):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    trial = ready_trial(driver, scenario_id)
    lab.trial_path.write_text(json.dumps(trial))
    patches = []
    monkeypatch.setattr(lab, "containment", lambda **kwargs: {})
    monkeypatch.setattr(lab, "deployment", lambda name: healthy(driver))
    monkeypatch.setattr(lab, "active_loads", lambda: [{"Id": "load-1"}])
    monkeypatch.setattr(
        lab,
        "healthy_target",
        lambda deployment: {"target_uid": "original", "image_digests": trial["image_digests"]},
    )
    monkeypatch.setattr(lab, "verify", lambda *args: {})
    monkeypatch.setattr(lab, "target_metric_snapshot", lambda *args: {})
    monkeypatch.setattr(lab, "offered_rate_snapshot", lambda uid, rate: {"observed_rate_rps": 8})
    monkeypatch.setattr(lab, "source_binding", lambda: {"sha256": "b" * 64})
    monkeypatch.setattr(lab, "patch", lambda resource, changes: patches.extend(changes))
    lab.fault(operation)

    record = json.loads((lab.directory / "fault.json").read_text())
    before, expected = record["before"], record["expected"]
    if operation == "memory-limit":
        assert expected["resources"]["limits"]["memory"] == driver.FAULT_MEMORY_LIMIT
        assert expected["resources"]["requests"]["memory"] == driver.FAULT_MEMORY_LIMIT
        assert expected["image"] == before["image"]
    elif operation == "bad-rollout":
        assert expected["image"].endswith(driver.RECOVERY_IMAGES["bad-image"])
        assert expected["resources"] == before["resources"]
    else:
        assert expected == before and patches == []
        # The overload began with its fixed load, not with this bookkeeping step.
        stored = json.loads(lab.trial_path.read_text())
        assert stored["phases"]["inject"] == trial["load"]["started_at"]
    assert expected["mode"] is None and record["trial_id"] == trial["trial_id"]


def action(driver, *, replicas=1, memory="256Mi", request=None, image=None):
    template = driver.owned_fields(healthy(driver))
    template["resources"]["limits"]["memory"] = memory
    if request:
        template["resources"]["requests"]["memory"] = request
    if image:
        template["image"] = image
    return {"replicas": replicas, "template": template, "rolling_update": {}}


@pytest.mark.parametrize(
    ("scenario_id", "state", "message"),
    [
        ("recovery-traffic-scale", {"replicas": 1}, "scale within"),
        ("recovery-oom-memory", {"memory": "128Mi", "request": "64Mi"}, "lift the injected"),
        ("recovery-bad-rollout-restore", {"image": "docker.io/library/other"}, "captured image"),
    ],
)
def test_qualified_states_must_repair_the_mapped_fault(
    driver, lab, monkeypatch, scenario_id, state, message
):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    with pytest.raises(RuntimeError, match=message):
        lab.validate_qualified_recovery_action(
            scenario_id, healthy(driver), action(driver, **state)
        )


@pytest.mark.parametrize(
    ("scenario_id", "state"),
    [
        ("recovery-traffic-scale", {"replicas": 2}),
        ("recovery-oom-memory", {}),
        ("recovery-bad-rollout-restore", {}),
    ],
)
def test_qualified_states_accept_the_e07_profiles(driver, lab, monkeypatch, scenario_id, state):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    lab.validate_qualified_recovery_action(scenario_id, healthy(driver), action(driver, **state))


def test_writer_opens_only_for_an_observed_recovery_trial(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    monkeypatch.setattr(lab, "maintenance", lambda *args: pytest.fail("Unexpected writer change"))
    with pytest.raises(RuntimeError, match="active trial"):
        lab.writer("enable")
    lab.trial_path.write_text(json.dumps(ready_trial(driver, "recovery-traffic-scale")))
    with pytest.raises(RuntimeError, match="observed"):
        lab.writer("enable")


def test_restored_baseline_clears_an_unfinished_recovery_trial(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    trial = ready_trial(driver, "recovery-oom-memory")
    deployment = healthy(driver)
    fields = driver.owned_fields(deployment)
    trial.update(
        status="observed",
        scope={
            "project": driver.OWNED_PROJECT,
            "namespace": "demo",
            "scope_manifest_sha256": "c" * 64,
        },
        declared_resources={"replicas": 1, "template": fields, "rolling_update": {}},
        surge_headroom={},
        baseline_at="2026-01-01T00:00:00+00:00",
        source_limits={},
        qualification=None,
        load={"state": "interrupted", "container_id": "load-1", "rate": 8},
        baseline={"owned_fields": fields, "pod_uids": ["pod-1"]},
    )
    lab.trial_path.write_text(json.dumps(trial))
    faulted = copy.deepcopy(fields)
    faulted["resources"]["limits"]["memory"] = driver.FAULT_MEMORY_LIMIT
    (lab.directory / "fault.json").write_text(
        json.dumps(
            {
                "uid": "original",
                "before": fields,
                "expected": faulted,
                "trial_id": trial["trial_id"],
                "run_id": trial["run_id"],
                "window_id": trial["window_id"],
                "source_binding_sha256": "b" * 64,
                "observation_sha256": "d" * 64,
            }
        )
    )
    receipts = []
    monkeypatch.setattr(lab, "deployment", lambda name: deployment)
    monkeypatch.setattr(lab, "active_loads", lambda: [])
    monkeypatch.setattr(lab, "wait_settled", lambda: None)
    monkeypatch.setattr(lab, "readiness_load", lambda **kwargs: {"responses": {"200": 200}})
    monkeypatch.setattr(lab, "verify", lambda *args: {})
    monkeypatch.setattr(lab, "containment", lambda **kwargs: {})
    monkeypatch.setattr(
        lab,
        "healthy_target",
        lambda live: {
            "target_uid": "original",
            "pod_uids": ["pod-2"],
            "image_digests": trial["image_digests"],
        },
    )
    monkeypatch.setattr(lab, "target_metric_snapshot", lambda *args: {})
    monkeypatch.setattr(
        lab,
        "source_binding",
        lambda: {"sha256": "b" * 64, "scope_manifest_sha256": "c" * 64, "files": {}},
    )
    monkeypatch.setattr(
        lab,
        "surge_headroom",
        lambda live: {
            "required_cpu_millicores": 50,
            "available_cpu_millicores": 1000,
            "required_memory_bytes": 1,
            "available_memory_bytes": 2,
            "qualified": True,
        },
    )
    monkeypatch.setattr(driver, "validate_trial_receipt", lambda value: receipts.append(value))
    lab.reset(interrupted=True)

    assert receipts[-1]["receipt_status"] == "incomplete"
    assert "positive E07 post-check" in receipts[-1]["incomplete_reason"]
    assert receipts[-1]["evidence"]["lab_state"] == "restored_baseline"
    assert not lab.trial_path.exists() and not (lab.directory / "fault.json").exists()


def test_no_other_targeted_load_during_a_recovery_trial(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    lab.trial_path.write_text(json.dumps(ready_trial(driver, "recovery-traffic-scale")))
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: pytest.fail("Unexpected load"))
    with pytest.raises(RuntimeError, match="owns the target's offered load"):
        lab.load("checkout", 30, 5)
    lab.trial_path.unlink()
    monkeypatch.setattr(lab, "active_loads", lambda: [{"Id": "other"}])
    with pytest.raises(RuntimeError, match="already running"):
        lab.load("checkout", 30, 5)


def test_recovery_load_must_use_the_qualified_rate(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    trial = ready_trial(driver, "recovery-traffic-scale")
    trial["load"] = None
    trial["qualification"] = {"rate_rps": 30}
    lab.trial_path.write_text(json.dumps(trial))
    monkeypatch.setattr(lab, "active_loads", lambda: [])
    monkeypatch.setattr(lab, "start_offered_load", lambda *args: pytest.fail("Unexpected load"))
    with pytest.raises(RuntimeError, match="exact rate"):
        lab.load("checkout", 900, 8, recovery=True, detach=True)


def test_qualification_covers_the_full_profile_window(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    monkeypatch.setattr(lab, "active_loads", lambda: [])
    monkeypatch.setattr(
        lab,
        "qualified_profile",
        lambda scenario_id: ({"window_seconds": 300, "warmup_seconds": 60}, "e" * 64),
    )
    monkeypatch.setattr(lab, "patch", lambda *args: pytest.fail("Unexpected write"))
    with pytest.raises(RuntimeError, match="profile window"):
        lab.qualify("recovery-oom-memory", 8, 240)


def test_metric_read_time_does_not_drift_past_finite_qualification_load(lab, driver, monkeypatch):
    deployment = healthy(driver)
    profile = {
        "window_seconds": 300,
        "warmup_seconds": 60,
        "step_seconds": 30,
        "containers": [{"resources": copy.deepcopy(driver.RECOVERY_RESOURCES)}],
        "replicas": [1],
        "source_age_seconds": 120,
        "minimum_rps": 6.4,
        "minimum_load_ratio": 0.9,
        "maximum_error_ratio": 0.01,
        "maximum_p95_seconds": 0.5,
    }
    clock, starts = [0.0], []

    def sleep(seconds):
        clock[0] += seconds

    def sample(*args):
        starts.append(clock[0])
        clock[0] += 4  # Actual metric reads consume part of the finite offered load.
        return dict(
            serving_pods=1,
            resets=0,
            sample_time=1,
            request_rate=8,
            offered_rate=8,
            error_ratio=0,
            p95_latency=0.24,
        )

    for name, result in {
        "require_idle_recovery_lab": None,
        "qualified_profile": (profile, "e" * 64),
        "deployment": deployment,
        "recovery_healthy_fields": driver.owned_fields(deployment),
        "healthy_target": {"target_uid": "original", "image_digests": ["sha256:" + "a" * 64]},
        "validate_qualified_recovery_action": None,
        "containment": {},
        "source_binding": {"sha256": "b" * 64},
        "wait_settled": None,
        "start_offered_load": ("finite-load", {"State": {"Running": True}}),
        "command": None,
        "recovery_surge_demand": {},
    }.items():
        monkeypatch.setattr(lab, name, lambda *args, result=result, **kwargs: result)
    monkeypatch.setattr(lab, "profile_sample", sample)
    monkeypatch.setattr(driver.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(driver.time, "sleep", sleep)
    monkeypatch.setattr(driver, "validate_qualification_receipt", lambda value: value)
    lab.qualify("recovery-oom-memory", 8, 330)
    assert starts == list(range(60, 301, 30))
    assert clock[0] < 330


def test_recovery_audit_retains_owned_writes_without_secret_bodies(driver):
    overlay = driver.yaml.safe_load((ROOT / "compose.lab-recovery.yaml").read_text())
    initializer = overlay["services"]["lab-audit-init"]
    assert initializer["network_mode"] == "none"
    assert initializer["read_only"] and initializer["cap_drop"] == ["ALL"]
    assert set(initializer["volumes"]) == {
        "k3s_data:/data",
        "./config/lab/recovery-audit-policy.yaml:/policy.yaml:ro",
    }
    command = overlay["services"]["k3s"]["command"]
    assert any("audit-policy-file=" in arg for arg in command)
    assert "--kube-apiserver-arg=audit-log-path=/var/log/cloudops-recovery-audit.jsonl" in command
    policy = driver.yaml.safe_load((ROOT / "config/lab/recovery-audit-policy.yaml").read_text())
    secrets, owned, mutations, default = policy["rules"]
    assert secrets["level"] == "Metadata"
    assert secrets["resources"] == [{"group": "", "resources": ["secrets"]}]
    assert owned["level"] == "RequestResponse" and owned["namespaces"] == ["demo"]
    assert owned["resources"] == [
        {"group": "apps", "resources": ["deployments", "deployments/scale"]}
    ]
    assert mutations["level"] == "Metadata"
    assert {"create", "update", "patch", "delete", "deletecollection"} <= set(mutations["verbs"])
    assert default["level"] == "None"


@pytest.mark.parametrize(
    ("stored", "matches"),
    [
        ("healthy", True),
        ("other-image", False),
        ("missing", False),
    ],
)
def test_known_good_baseline_must_equal_the_healthy_sample(
    lab, driver, monkeypatch, stored, matches
):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    healthy_fields = lab.recovery_healthy_fields()
    row = {
        "id": str(uuid4()),
        "digest": "f" * 64,
        "confirmed_at": "2026-01-01T00:00:00+00:00",
        "uid": "original",
        "containers": [
            {
                "name": "app",
                "image": healthy_fields["image"] if stored == "healthy" else "docker.io/library/x",
                "resources": healthy_fields["resources"],
            }
        ],
    }
    output = json.dumps(None if stored == "missing" else row)
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: output + "\n")
    result = lab.known_good_baseline("original")
    assert (result is not None) is matches
    if matches:
        assert result == {key: row[key] for key in ("id", "digest", "confirmed_at")}


def test_incomplete_receipt_does_not_count_the_trial_load_as_a_duplicate(lab, driver, monkeypatch):
    trial = ready_trial(driver, "recovery-traffic-scale")
    trial.update(
        scope={},
        declared_resources={},
        surge_headroom={},
        baseline_at="",
        source_limits={},
    )
    monkeypatch.setattr(lab, "active_loads", lambda: [{"Id": "load-1"}, {"Id": "other"}])
    receipts = []
    monkeypatch.setattr(driver, "validate_trial_receipt", lambda value: receipts.append(value))
    lab.incomplete_trial_reset(trial, "Recovery offered load must be finalized before reset.")
    assert receipts[-1]["duplicate_targeted_loads"] == 1


def test_probe_incident_request_carries_no_scenario_or_control_text():
    spec = importlib.util.spec_from_file_location(
        "probe_e10_s3", ROOT / "scripts/probe-e10-s3-recovery.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    text = json.dumps(probe.incident_request("workload-id")).lower()
    catalog = json.loads((ROOT / "config/lab/release-scenarios-v1.json").read_text())
    controls = json.loads(
        (ROOT / "services/backend/tests/fixtures/e10_s3/controls.json").read_text()
    )
    words = {item["id"] for item in catalog["scenarios"]}
    for control in controls["controls"].values():
        words.update(str(value) for value in control.values() if isinstance(value, str))
    words |= {"e10", "trial", "scenario", "fault", "oom", "overload", "bad-image", "rollout"}
    assert not [word for word in words if word.lower() in text]


def _open_target(epoch="epoch-1"):
    return {"uid": "original", "epoch": epoch, "enabled": True, "recovery_mode": False}


def test_writer_opens_at_the_healthy_baseline_and_records_its_epoch(lab, driver, monkeypatch):
    monkeypatch.setattr(driver, "RECOVERY_LAB", True)
    trial = ready_trial(driver, "recovery-oom-memory")
    trial.update(target_uid="original", load={"state": "running", "container_id": "load-1"})
    lab.trial_path.write_text(json.dumps(trial))
    monkeypatch.setattr(lab, "active_loads", lambda: [{"Id": "load-1"}])
    monkeypatch.setattr(
        lab, "dc", lambda *args, **kwargs: json.dumps({"permit": {"mode": "recovery"}})
    )
    monkeypatch.setattr(lab, "dispatch", lambda *args: None)
    calls = []

    def maintenance(operation="status"):
        calls.append(operation)
        if operation == "status":
            return {"targets": [_open_target()]}
        return {"status": "applied"}

    monkeypatch.setattr(lab, "maintenance", maintenance)
    lab.writer("enable", at_baseline=True)
    assert calls == ["enable_target", "exit_recovery_mode", "status"]
    recorded = json.loads(lab.trial_path.read_text())["writer_baseline"]
    assert recorded["target_uid"] == "original" and recorded["epoch"] == "epoch-1"
    # Once per trial, and never after the fault.
    with pytest.raises(RuntimeError, match="ready recovery trial before its fault"):
        lab.writer("enable", at_baseline=True)
    trial["status"] = "injected"
    lab.trial_path.write_text(json.dumps(trial))
    with pytest.raises(RuntimeError, match="ready recovery trial before its fault"):
        lab.writer("enable", at_baseline=True)


def test_fault_accepts_only_the_same_baseline_writer(lab, driver, monkeypatch):
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: "0")
    trial = {"writer_baseline": {"target_uid": "original", "epoch": "epoch-1"}}
    status = {"targets": [_open_target()], "unknown_attributions": []}
    monkeypatch.setattr(lab, "maintenance", lambda *args: status)
    driver.Lab.no_execution(lab, baseline_writer=trial)
    with pytest.raises(RuntimeError, match="Writer must be quarantined"):
        driver.Lab.no_execution(lab)
    status["targets"] = [_open_target("epoch-2")]
    with pytest.raises(RuntimeError, match="Writer must be quarantined"):
        driver.Lab.no_execution(lab, baseline_writer=trial)
    status["targets"] = [_open_target()]
    status["unknown_attributions"] = [{"id": "a", "safety_exclusion_request": None}]
    with pytest.raises(RuntimeError, match="Writer must be quarantined"):
        driver.Lab.no_execution(lab, baseline_writer=trial)
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: "1")
    with pytest.raises(RuntimeError, match="Active or unresolved execution"):
        driver.Lab.no_execution(lab, baseline_writer=trial)
