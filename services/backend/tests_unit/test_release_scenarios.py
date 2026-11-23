"""E10 S3 receipt and post-check validation without a live lab."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def scenarios(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "services/backend/src"))
    spec = importlib.util.spec_from_file_location(
        "release_scenarios_test", ROOT / "scripts/release_scenarios.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template(cpu="100m", memory="64Mi"):
    return {
        "image": "registry.local/checkout@sha256:" + "a" * 64,
        "resources": {
            "requests": {"cpu": cpu, "memory": memory},
            "limits": {"cpu": "400m", "memory": "160Mi"},
        },
        "imagePullPolicy": "IfNotPresent",
        "mode": None,
        "initContainers": None,
    }


def receipt(scenarios, *, recovery=False):
    now = "2026-01-01T00:00:00+00:00"
    declared = {
        "replicas": 1,
        "template": template(),
        "rolling_update": {"maxSurge": 1, "maxUnavailable": 0},
    }
    if recovery:
        declared["qualified_action"] = {
            "replicas": 1,
            "template": template(),
            "rolling_update": {"maxSurge": 1, "maxUnavailable": 0},
        }
        declared["qualification_profile_digest"] = "b" * 64
    demand = scenarios._validate_declared_resources(declared)
    return {
        "schema": "E10TrialResetReceipt/v1",
        "trial_id": str(uuid4()),
        "run_id": str(uuid4()),
        "window_id": str(uuid4()),
        "reset_id": str(uuid4()),
        "scenario_id": "recovery-oom-memory" if recovery else "pending-insufficient-cpu",
        "reset_state": "faulted" if recovery else "healthy",
        "scope": {
            "project": "cloudops-local-testing-lab",
            "namespace": "demo",
            "scope_manifest_sha256": "c" * 64,
        },
        "target_uid": "target-uid",
        "source_binding_sha256": "d" * 64,
        "image_digests": ["containerd://sha256:" + "e" * 64],
        "declared_resources": declared,
        "surge_headroom": {
            **demand,
            "available_cpu_millicores": 1000,
            "available_memory_bytes": 1024 * 1024 * 1024,
            "qualified": True,
        },
        "baseline_at": now,
        "observed_at": now,
        "source_limits": {
            "request_timeout_seconds": 8,
            "response_bytes": 2_000_000,
            "series_limit": 25,
        },
        "residual_eligible_actions": 0,
        "duplicate_targeted_loads": 0,
        "receipt_status": "reset_complete",
        "phases": {"baseline": now, "reset": now},
        "evidence": {},
    }


def test_completed_receipt_rejects_empty_declared_resources(scenarios):
    value = receipt(scenarios)
    value["declared_resources"] = {}
    value["surge_headroom"].update(
        required_cpu_millicores=0,
        required_memory_bytes=0,
        qualified=True,
    )

    with pytest.raises(ValueError, match="Declared resources"):
        scenarios.validate_trial_receipt(value)


def test_completed_receipt_rejects_forged_zero_surge_demand(scenarios):
    value = receipt(scenarios)
    value["surge_headroom"].update(
        required_cpu_millicores=0,
        required_memory_bytes=0,
        qualified=True,
    )

    with pytest.raises(ValueError, match="surge demand"):
        scenarios.validate_trial_receipt(value)


def test_qualified_action_demand_includes_added_replica_and_surge(scenarios):
    value = receipt(scenarios)
    value["scenario_id"] = "recovery-oom-memory"
    action = {
        "replicas": 2,
        "template": template(cpu="250m", memory="100Mi"),
        "rolling_update": {"maxSurge": 1, "maxUnavailable": 0},
    }
    value["declared_resources"]["qualified_action"] = action
    value["declared_resources"]["qualification_profile_digest"] = "b" * 64
    demand = scenarios._validate_declared_resources(value["declared_resources"])
    value["surge_headroom"].update(demand)

    assert demand == {
        "required_cpu_millicores": 500,
        "required_memory_bytes": 200 * 1024 * 1024,
    }


def test_completed_recovery_receipt_requires_positive_post_check(scenarios):
    value = receipt(scenarios, recovery=True)

    with pytest.raises(ValueError, match="post-check attestation"):
        scenarios.validate_trial_receipt(value)


def test_completed_recovery_receipt_accepts_bound_post_check(scenarios):
    value = receipt(scenarios, recovery=True)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    acknowledged = started + timedelta(minutes=2)
    deadline = acknowledged + timedelta(seconds=180)
    observed = deadline + timedelta(seconds=30)
    stopped = observed + timedelta(seconds=30)
    observation_digest = "f" * 64
    fault_treatment = {"mode": "oom"}
    load = {
        "state": "stopped_after_post_check",
        "seconds": 900,
        "rate": 10,
        "started_at": started.isoformat(),
        "finished_at": stopped.isoformat(),
        "observations": [
            {
                "phase": "positive-post-check",
                "deployment_uid": value["target_uid"],
                "expected_rate_rps": 10,
                "observed_rate_rps": 10.0,
                "sample_at": str(observed.timestamp()),
            }
        ],
    }
    value["observed_at"] = stopped.isoformat()
    value["evidence"] = {
        "fault": {
            "expected": fault_treatment,
            "observation_sha256": observation_digest,
            "observed_at": (started + timedelta(minutes=1)).isoformat(),
        },
        "offered_load": load,
    }
    value["post_check"] = {
        "schema": "E10RecoveryPostCheck/v1",
        "status": "passed",
        "trial_id": value["trial_id"],
        "run_id": value["run_id"],
        "window_id": value["window_id"],
        "target_uid": value["target_uid"],
        "execution_id": str(uuid4()),
        "verification_profile_digest": "b" * 64,
        "execution_receipt_sha256": "1" * 64,
        "verification_receipt_sha256": "2" * 64,
        "fault_observation_sha256": observation_digest,
        "fault_treatment_sha256": scenarios._canonical_sha256(fault_treatment),
        "action_state": value["declared_resources"]["qualified_action"],
        "load_started_at": started.isoformat(),
        "verification_acknowledged_at": acknowledged.isoformat(),
        "verification_deadline": deadline.isoformat(),
        "verification_finalized_at": deadline.isoformat(),
        "verification_window_seconds": 180,
        "expected_rollout_samples": 5,
        "valid_rollout_samples": 5,
        "post_check_observed_at": observed.isoformat(),
        "offered_sample_at": observed.isoformat(),
        "load_stopped_at": stopped.isoformat(),
        "load_final_state": "stopped_after_post_check",
        "requested_rate_rps": 10,
        "observed_rate_rps": 10.0,
        "offered_window_rate_rps": 10.0,
    }

    assert scenarios.validate_trial_receipt(value)["receipt_status"] == "reset_complete"


def e07_receipts():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    acknowledged = start + timedelta(minutes=2)
    deadline = acknowledged + timedelta(seconds=180)
    execution_id = str(uuid4())
    result = {
        "state": "passed",
        "expected_rollout_samples": 5,
        "valid_rollout_samples": 5,
        "application": {"state": "passed", "offered_rps": 10.0},
    }
    execution = {
        "id": execution_id,
        "state": "succeeded",
        "finished_at": deadline.isoformat(),
        "result": {
            "verification": "passed",
            "verification_finalized_at": deadline.isoformat(),
            "verification_profile_digest": "b" * 64,
            "verification_result": deepcopy(result),
        },
    }
    verification = {
        "execution_id": execution_id,
        "profile_digest": "b" * 64,
        "profile": {
            "workload_uid": "target-uid",
            "template": "set_memory_profile",
            "window_seconds": 180,
            "warmup_seconds": 60,
            "step_seconds": 30,
        },
        "acknowledged_at": acknowledged.isoformat(),
        "deadline": deadline.isoformat(),
        "finalized_at": deadline.isoformat(),
        "next_slot": 7,
        "result": result,
    }
    load = {
        "state": "running",
        "seconds": 900,
        "rate": 10,
        "started_at": start.isoformat(),
    }
    return execution, verification, load, start


def test_recovery_post_check_accepts_complete_fixed_window(scenarios):
    execution, verification, load, start = e07_receipts()

    result = scenarios.validate_recovery_post_check(
        execution,
        verification,
        scenario_id="recovery-oom-memory",
        target_uid="target-uid",
        profile_digest="b" * 64,
        load=load,
        fault_observed_at=(start + timedelta(minutes=1)).isoformat(),
    )

    assert result["status"] == "passed"
    assert result["valid_rollout_samples"] == 5


@pytest.mark.parametrize(
    "mutation",
    ["failed-execution", "wrong-target", "incomplete-window", "low-offered-rate"],
)
def test_recovery_post_check_rejects_unqualified_evidence(scenarios, mutation):
    execution, verification, load, start = e07_receipts()
    if mutation == "failed-execution":
        execution["state"] = "failed"
    elif mutation == "wrong-target":
        verification["profile"]["workload_uid"] = "other-uid"
    elif mutation == "incomplete-window":
        verification["result"]["valid_rollout_samples"] = 4
        execution["result"]["verification_result"]["valid_rollout_samples"] = 4
    elif mutation == "low-offered-rate":
        verification["result"]["application"]["offered_rps"] = 1.0
        execution["result"]["verification_result"]["application"]["offered_rps"] = 1.0

    with pytest.raises(ValueError):
        scenarios.validate_recovery_post_check(
            execution,
            verification,
            scenario_id="recovery-oom-memory",
            target_uid="target-uid",
            profile_digest="b" * 64,
            load=load,
            fault_observed_at=(start + timedelta(minutes=1)).isoformat(),
        )
