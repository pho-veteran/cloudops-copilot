#!/usr/bin/env python3
"""Versioned E10 S3 fixture and oracle boundaries."""

import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import re
from uuid import UUID

from cloudops.features.workloads.kubernetes import pod_requests, quantity
from cloudops.shared.errors import DomainError

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/lab/release-scenarios-v1.json"
RECEIPT_SCHEMA = ROOT / "config/lab/e10-s3-trial-reset-receipt.schema.json"
FORBIDDEN_INPUT_KEYS = {
    "allowed_action",
    "allowed_candidates",
    "case_id",
    "control_ref",
    "expected",
    "eligible_action",
    "expected_action",
    "expected_category",
    "expected_result",
    "ground_truth",
    "oracle",
    "provider_states",
    "reset_id",
    "scenario_id",
    "truth",
}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot load JSON artifact {path}: {type(error).__name__}"
        ) from None


def _walk_keys(value):
    if isinstance(value, dict):
        yield from value
        for child in value.values():
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _parse_timestamp(value, name):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid timestamp {name}.") from None
    _require(stamp.tzinfo is not None, f"Timestamp {name} must be timezone-aware.")
    return stamp


def _validate_resource_template(template):
    _require(
        isinstance(template, dict)
        and set(template)
        == {"image", "resources", "imagePullPolicy", "mode", "initContainers"},
        "Declared resource template is incomplete or has unexpected fields.",
    )
    _require(
        isinstance(template["image"], str)
        and bool(template["image"])
        and isinstance(template["imagePullPolicy"], str)
        and bool(template["imagePullPolicy"]),
        "Declared image or pull policy is missing.",
    )
    _require(
        template["mode"] is None
        or (
            isinstance(template["mode"], dict)
            and set(template["mode"]) == {"name", "value"}
            and template["mode"]["name"] == "LAB_MODE"
            and isinstance(template["mode"]["value"], str)
        ),
        "Declared scenario mode is invalid.",
    )
    init_containers = template["initContainers"]
    _require(
        init_containers is None or isinstance(init_containers, list),
        "Declared init-container state is invalid.",
    )
    _require(
        all(isinstance(item, dict) for item in init_containers or []),
        "Declared init containers must be objects.",
    )
    resources = template["resources"]
    _require(
        isinstance(resources, dict) and set(resources) == {"requests", "limits"},
        "Declared resources must include requests and limits.",
    )
    for container_resources in [
        resources,
        *(item.get("resources", {}) for item in init_containers or []),
    ]:
        _require(
            isinstance(container_resources, dict)
            and set(container_resources) == {"requests", "limits"},
            "Every declared container must include requests and limits.",
        )
        for group in ("requests", "limits"):
            values = container_resources[group]
            _require(
                isinstance(values, dict) and set(values) == {"cpu", "memory"},
                "Declared CPU and memory requests/limits are required.",
            )
            for resource in ("cpu", "memory"):
                _require(
                    isinstance(values[resource], (str, int, float))
                    and not isinstance(values[resource], bool),
                    "Declared resource quantities must be numeric Kubernetes quantities.",
                )
                try:
                    amount = quantity(values[resource])
                except (DomainError, TypeError, ValueError):
                    raise ValueError("Declared resource quantity is invalid.") from None
                _require(amount > 0, "Declared resource quantities must be positive.")
        for resource in ("cpu", "memory"):
            try:
                request = quantity(container_resources["requests"][resource])
                limit = quantity(container_resources["limits"][resource])
            except (DomainError, TypeError, ValueError):
                raise ValueError("Declared resource quantity is invalid.") from None
            _require(request <= limit, "Declared resource request exceeds its limit.")
    try:
        requests = pod_requests(
            {
                "containers": [{"resources": resources}],
                "initContainers": init_containers or [],
            }
        )
    except (DomainError, KeyError, TypeError, ValueError):
        raise ValueError("Declared Pod requests cannot be evaluated safely.") from None
    return requests


def _surge_replicas(rolling_update, replicas):
    _require(
        isinstance(rolling_update, dict)
        and set(rolling_update) <= {"maxSurge", "maxUnavailable"},
        "Declared rollout settings are invalid.",
    )
    value = rolling_update.get("maxSurge", 1)
    if type(value) is int:
        surge = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}%", value):
        surge = math.ceil(replicas * int(value[:-1]) / 100)
    else:
        raise ValueError("Declared maxSurge is not a bounded integer or percentage.")
    _require(0 <= surge <= 3, "Declared maxSurge exceeds the owned-lab bound.")
    return surge


def _resource_demand(state):
    replicas = state["replicas"]
    requests = _validate_resource_template(state["template"])
    surge = _surge_replicas(state["rolling_update"], replicas)
    return {
        "required_cpu_millicores": math.ceil(requests["cpu"] * surge * 1000),
        "required_memory_bytes": int(requests["memory"] * surge),
    }


def _validate_declared_resources(value):
    _require(
        isinstance(value, dict)
        and {"replicas", "template", "rolling_update"} <= set(value)
        and set(value)
        <= {
            "replicas",
            "template",
            "rolling_update",
            "qualified_action",
            "qualification_profile_digest",
        },
        "Declared resources must include the exact baseline rollout and template.",
    )
    _require(
        type(value["replicas"]) is int and 1 <= value["replicas"] <= 3,
        "Declared baseline replicas are outside the owned-lab bound.",
    )
    baseline = {
        "replicas": value["replicas"],
        "template": value["template"],
        "rolling_update": value["rolling_update"],
    }
    expected = _resource_demand(baseline)
    has_action = "qualified_action" in value
    has_digest = "qualification_profile_digest" in value
    _require(
        has_action == has_digest,
        "Qualified action and profile digest must be present together.",
    )
    if has_action:
        action = value["qualified_action"]
        _require(
            isinstance(action, dict)
            and set(action) == {"replicas", "template", "rolling_update"}
            and type(action["replicas"]) is int
            and 1 <= action["replicas"] <= 3,
            "Qualified action resources are invalid.",
        )
        _require(
            action["rolling_update"] == value["rolling_update"],
            "Qualified action rollout settings differ from the captured baseline.",
        )
        _require(
            isinstance(value["qualification_profile_digest"], str)
            and re.fullmatch(r"[a-f0-9]{64}", value["qualification_profile_digest"])
            is not None,
            "Qualified action profile digest is invalid.",
        )
        baseline_surge = _surge_replicas(value["rolling_update"], action["replicas"])
        action_requests = _validate_resource_template(action["template"])
        action_units = max(0, action["replicas"] - value["replicas"]) + baseline_surge
        expected = {
            "required_cpu_millicores": max(
                expected["required_cpu_millicores"],
                math.ceil(action_requests["cpu"] * action_units * 1000),
            ),
            "required_memory_bytes": max(
                expected["required_memory_bytes"],
                int(action_requests["memory"] * action_units),
            ),
        }
    return expected


def validate_recovery_post_check(
    execution,
    verification,
    *,
    scenario_id,
    target_uid,
    profile_digest,
    load,
    fault_observed_at,
):
    """Validate the exact positive E07 verification result before stopping its offered load."""
    expected_templates = {
        "recovery-oom-memory": "set_memory_profile",
        "recovery-bad-rollout-restore": "restore_deployment_template",
        "recovery-traffic-scale": "scale_deployment",
    }
    _require(
        scenario_id in expected_templates, "Post-check scenario is not a recovery case."
    )
    _require(
        isinstance(execution, dict), "Recovery execution receipt must be an object."
    )
    _require(
        isinstance(verification, dict),
        "Recovery verification receipt must be an object.",
    )
    execution_id = execution.get("id")
    try:
        UUID(execution_id)
        UUID(verification.get("execution_id"))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Post-check execution identity must be a UUID.") from None
    _require(
        verification["execution_id"] == execution_id,
        "Execution and verification identities differ.",
    )
    result = execution.get("result")
    verification_result = verification.get("result")
    profile = verification.get("profile")
    _require(isinstance(result, dict), "Execution result is missing.")
    _require(isinstance(verification_result, dict), "Verification result is missing.")
    _require(isinstance(profile, dict), "Verification profile is missing.")
    _require(
        execution.get("state") == "succeeded"
        and result.get("verification") == "passed"
        and verification_result.get("state") == "passed"
        and result.get("verification_result") == verification_result,
        "E07 execution did not finish with a positive verification.",
    )
    _require(
        profile.get("workload_uid") == target_uid,
        "E07 verification profile is bound to a different target UID.",
    )
    _require(
        profile.get("template") == expected_templates[scenario_id],
        "E07 verification template does not match the recovery scenario.",
    )
    _require(
        verification.get("profile_digest") == profile_digest
        and result.get("verification_profile_digest") == profile_digest
        and re.fullmatch(r"[a-f0-9]{64}", str(profile_digest)) is not None,
        "E07 verification profile digest does not match qualification.",
    )
    window_seconds = profile.get("window_seconds")
    warmup_seconds = profile.get("warmup_seconds")
    step_seconds = profile.get("step_seconds")
    _require(
        type(window_seconds) is int
        and window_seconds in {180, 300}
        and warmup_seconds == 60
        and step_seconds == 30,
        "E07 verification does not use the qualified fixed observation window.",
    )
    acknowledged_at = _parse_timestamp(
        verification.get("acknowledged_at"), "verification acknowledged_at"
    )
    deadline = _parse_timestamp(verification.get("deadline"), "verification deadline")
    finalized_at = _parse_timestamp(
        verification.get("finalized_at"), "verification finalized_at"
    )
    execution_finished_at = _parse_timestamp(
        execution.get("finished_at"), "execution finished_at"
    )
    fault_at = _parse_timestamp(fault_observed_at, "fault observed_at")
    load_started_at = _parse_timestamp(
        load.get("started_at"), "offered load started_at"
    )
    _require(
        deadline == acknowledged_at + timedelta(seconds=window_seconds)
        and finalized_at >= deadline
        and execution_finished_at == finalized_at
        and _parse_timestamp(
            result.get("verification_finalized_at"), "execution verification time"
        )
        == finalized_at,
        "E07 verification does not prove its complete fixed window.",
    )
    _require(
        load.get("state") == "running"
        and type(load.get("seconds")) is int
        and 1 <= load["seconds"] <= 900
        and type(load.get("rate")) is int
        and 1 <= load["rate"] <= 30
        and load_started_at <= fault_at <= acknowledged_at
        and load_started_at + timedelta(seconds=load["seconds"]) >= deadline,
        "The same bounded offered load did not span fault detection and E07 verification.",
    )
    expected_samples = (window_seconds - warmup_seconds) // step_seconds + 1
    application = verification_result.get("application")
    _require(
        isinstance(application, dict), "Positive post-check lacks application evidence."
    )
    valid_samples = verification_result.get("valid_rollout_samples")
    _require(
        application.get("state") == "passed"
        and verification_result.get("expected_rollout_samples") == expected_samples
        and valid_samples == expected_samples
        and type(verification.get("next_slot")) is int
        and verification["next_slot"] == window_seconds // step_seconds + 1,
        "E07 positive post-check lacks its complete fixed-window sample set.",
    )
    offered_window_rate = application.get("offered_rps")
    _require(
        isinstance(offered_window_rate, (int, float))
        and math.isfinite(offered_window_rate)
        and load["rate"] * 0.8 <= offered_window_rate <= load["rate"] * 1.2,
        "E07 positive post-check does not prove the fixed offered rate.",
    )
    return {
        "schema": "E10RecoveryPostCheck/v1",
        "status": "passed",
        "execution_id": execution_id,
        "verification_profile_digest": profile_digest,
        "verification_acknowledged_at": acknowledged_at.isoformat(),
        "verification_deadline": deadline.isoformat(),
        "verification_finalized_at": finalized_at.isoformat(),
        "verification_window_seconds": window_seconds,
        "expected_rollout_samples": expected_samples,
        "valid_rollout_samples": valid_samples,
        "offered_window_rate_rps": float(offered_window_rate),
    }


def project_case_inputs(scenario_id):
    """Return only request and evidence payloads; omit IDs, labels, controls and oracle data."""
    catalog = load_catalog()
    scenarios = {item["id"]: item for item in catalog["scenarios"]}
    _require(scenario_id in scenarios, "Unknown scenario ID.")
    e06_cases = {
        case["id"]: case
        for case in read_json(ROOT / "services/backend/tests/fixtures/e06/cases.json")
    }
    e10_inputs = {
        case["id"]: case
        for case in read_json(
            ROOT / "services/backend/tests/fixtures/e10_s3/inputs.json"
        )["cases"]
    }
    projected = []
    for reference in scenarios[scenario_id]["input_refs"]:
        prefix, case_id = reference.split(":", 1)
        if prefix == "E06":
            case = e06_cases[case_id]
            payload = {
                "request": {"question": case["question"]},
                "evidence": case["evidence"],
            }
        else:
            case = e10_inputs[case_id]
            evidence_ref = case.get("evidence_ref")
            if evidence_ref:
                evidence_source, evidence_id = evidence_ref.split(":", 1)
                _require(
                    evidence_source == "E06", "Unsupported E10 evidence reference."
                )
                evidence = e06_cases[evidence_id]["evidence"]
            else:
                evidence = case.get("evidence", {})
            payload = {"request": case["request"], "evidence": evidence}
        _require(
            set(payload) == {"request", "evidence"},
            "Input projection includes metadata.",
        )
        _require(
            not (FORBIDDEN_INPUT_KEYS & set(_walk_keys(payload))),
            "Input projection includes labels or operator controls.",
        )
        projected.append(payload)
    return projected


def load_catalog():
    catalog = read_json(CATALOG)
    _require(
        catalog.get("schema") == "E10ReleaseScenarioCatalog/v1",
        "Unknown catalog schema.",
    )
    _require(catalog.get("version") == "1.0.0", "Unexpected catalog version.")
    _require(
        catalog.get("source_baseline") == "dc0dbb8b1a53d681c9ef9cd638b55651fbd4124f",
        "Pinned E10 baseline changed; review scenario qualification before updating.",
    )
    _require(
        catalog.get("evidence_contract") == "E10TrialResetReceipt/v1",
        "Unexpected reset receipt contract.",
    )
    return catalog


def validate_catalog():
    catalog = load_catalog()
    receipt_schema = read_json(ROOT / catalog["receipt_schema"])
    _require(
        sha256_file(ROOT / catalog["receipt_schema"])
        == catalog["receipt_schema_sha256"],
        "Trial reset receipt schema changed; review the D3 contract before updating.",
    )
    _require(
        receipt_schema.get("$id", "").endswith("E10TrialResetReceipt-v1.json"),
        "Unknown trial reset receipt schema.",
    )
    qualification_schema = read_json(ROOT / catalog["qualification_receipt_schema"])
    _require(
        sha256_file(ROOT / catalog["qualification_receipt_schema"])
        == catalog["qualification_receipt_schema_sha256"],
        "Qualification receipt schema changed; review its consumer contract before updating.",
    )
    _require(
        qualification_schema.get("$id", "").endswith(
            "E10RecoveryQualificationReceipt-v1.json"
        ),
        "Unknown recovery qualification receipt schema.",
    )
    sources = catalog["source_fixtures"]
    for name, entry in sources.items():
        path = ROOT / entry["path"]
        _require(path.is_file(), f"Missing pinned source fixture: {name}.")
        _require(
            sha256_file(path) == entry["sha256"],
            f"Pinned source fixture changed: {name}.",
        )

    oracle_path = ROOT / catalog["sealed_oracle"]["path"]
    _require(oracle_path.is_file(), "Sealed oracle is missing.")
    oracle_sha = sha256_file(oracle_path)
    _require(
        oracle_sha == catalog["sealed_oracle"]["sha256"],
        "Sealed oracle hash changed; a new frozen cohort is required.",
    )
    oracle = read_json(oracle_path)
    _require(oracle.get("schema") == "E10S3SealedOracle/v1", "Unknown oracle schema.")
    _require(oracle.get("frozen_before_measurement") is True, "Oracle is not frozen.")

    inputs_path = ROOT / sources["e10_s3_inputs"]["path"]
    inputs = read_json(inputs_path)
    _require(inputs.get("schema") == "E10S3CaseInputs/v1", "Unknown S3 input schema.")
    _require(
        not (FORBIDDEN_INPUT_KEYS & set(_walk_keys(inputs))),
        "Labels or operator controls leaked into case inputs.",
    )
    input_ids = {case["id"] for case in inputs["cases"]}
    _require(
        input_ids
        == {
            "recovery-bad-rollout-restore",
            "negative-insufficient-evidence",
            "negative-out-of-scope",
            "negative-provider-unavailable-quota",
        },
        "S3-only negative input set changed.",
    )

    controls = read_json(ROOT / sources["e10_s3_controls"]["path"])
    _require(
        controls.get("schema") == "E10S3OperatorControls/v1", "Unknown control schema."
    )
    control_ids = set(controls["controls"])
    expected_lab_faults = {
        "recovery-oom-memory": "oom",
        "recovery-bad-rollout-restore": "crashloop",
        "recovery-traffic-scale": "traffic",
        "pending-insufficient-cpu": "pending",
        "imagepull-missing-manifest": "imagepull",
    }
    for scenario_id, operation in expected_lab_faults.items():
        _require(
            controls["controls"][scenario_id].get("local_fault_operation") == operation,
            f"Scenario {scenario_id} does not map to its bounded owned-lab fault.",
        )
    scenarios = catalog["scenarios"]
    scenario_ids = {scenario["id"] for scenario in scenarios}
    _require(len(scenarios) == len(scenario_ids), "Duplicate scenario ID.")
    _require(
        scenario_ids == set(oracle["cases"]),
        "Catalog and sealed oracle case IDs differ.",
    )
    _require(
        scenario_ids == control_ids, "Catalog and operator control case IDs differ."
    )
    _require(
        scenario_ids
        == set(catalog["scenario_sets"]["recovery"])
        | set(catalog["scenario_sets"]["pending_imagepull"])
        | set(catalog["scenario_sets"]["negative"]),
        "Scenario set membership does not cover the catalog exactly.",
    )
    _require(
        len(catalog["scenario_sets"]["recovery"]) == 3,
        "Expected three recovery scenarios.",
    )
    _require(
        len(catalog["scenario_sets"]["pending_imagepull"]) == 4,
        "Expected two Pending and two ImagePull variants.",
    )
    _require(
        len(catalog["scenario_sets"]["negative"]) == 5, "Expected five negative cases."
    )
    _require(
        set(catalog["scenario_sets"]["recovery"])
        == {
            "recovery-oom-memory",
            "recovery-bad-rollout-restore",
            "recovery-traffic-scale",
        },
        "The three recovery scenario mappings changed.",
    )
    pending_variants = {
        item
        for item in catalog["scenario_sets"]["pending_imagepull"]
        if item.startswith("pending-")
    }
    imagepull_variants = {
        item
        for item in catalog["scenario_sets"]["pending_imagepull"]
        if item.startswith("imagepull-")
    }
    _require(
        len(pending_variants) == 2 and len(imagepull_variants) == 2,
        "Expected exactly two Pending and two ImagePull variants.",
    )
    _require(
        set(catalog["scenario_sets"]["negative"])
        == {
            "negative-healthy",
            "negative-insufficient-evidence",
            "negative-untrusted-log",
            "negative-out-of-scope",
            "negative-provider-unavailable-quota",
        },
        "The fixed negative set changed.",
    )

    e06_cases = read_json(ROOT / sources["e06_cases"]["path"])
    e06_oracle = read_json(ROOT / sources["e06_oracle"]["path"])
    e07_integrated = read_json(ROOT / sources["e07_integrated"]["path"])
    e06_case_ids = {case["id"] for case in e06_cases}
    e06_oracle_ids = set(e06_oracle["cases"])
    for scenario in scenarios:
        _require(
            scenario["control_ref"].startswith("E10S3:controls/"),
            "Invalid control reference.",
        )
        _require(
            scenario["control_ref"].split("/", 1)[1] in control_ids,
            f"Unknown operator control for {scenario['id']}.",
        )
        for reference in scenario["input_refs"]:
            prefix, case_id = reference.split(":", 1)
            if prefix == "E06":
                _require(
                    case_id in e06_case_ids, f"Unknown E06 input reference {reference}."
                )
                _require(
                    case_id in e06_oracle_ids,
                    f"Unknown E06 oracle reference {reference}.",
                )
            elif prefix == "E10S3":
                _require(
                    case_id in input_ids, f"Unknown E10 S3 input reference {reference}."
                )
            else:
                raise ValueError(f"Unknown input reference prefix {prefix}.")
        if scenario["kind"] == "recovery":
            _require(
                scenario["qualification_required"] is True,
                "Recovery scenario lacks qualification gate.",
            )
        _require(
            isinstance(oracle["cases"][scenario["id"]], dict), "Invalid oracle case."
        )
        evidence_ref = scenario.get("evidence_ref")
        if evidence_ref:
            _require(
                evidence_ref.startswith("E07:"),
                "Unexpected external evidence reference.",
            )
            path, pointer = evidence_ref[4:].split("#", 1)
            _require((ROOT / path).is_file(), f"Missing E07 evidence source {path}.")
            target = e07_integrated
            for part in pointer.strip("/").split("/"):
                _require(
                    isinstance(target, dict) and part in target,
                    "Invalid E07 evidence pointer.",
                )
                target = target[part]
            _require(
                bool(target), "E07 evidence reference resolves to an empty object."
            )

    for case in oracle["cases"].values():
        source_evidence_ref = case.get("source_evidence_ref")
        if source_evidence_ref:
            _require(
                source_evidence_ref.startswith("E07:"),
                "Unexpected oracle evidence source.",
            )
            path, pointer = source_evidence_ref[4:].split("#", 1)
            _require((ROOT / path).is_file(), f"Missing E07 evidence source {path}.")
            target = e07_integrated
            for part in pointer.strip("/").split("/"):
                _require(
                    isinstance(target, dict) and part in target,
                    "Invalid E07 oracle evidence pointer.",
                )
                target = target[part]
            _require(
                bool(target),
                "E07 oracle evidence reference resolves to an empty object.",
            )
        refs = case.get("source_oracle_refs", [])
        if "source_oracle_ref" in case:
            refs.append(case["source_oracle_ref"])
        for reference in refs:
            if reference.startswith("E06:/"):
                target = e06_oracle
                for part in reference[5:].strip("/").split("/"):
                    _require(
                        isinstance(target, dict) and part in target,
                        "Invalid E06 oracle pointer.",
                    )
                    target = target[part]
            elif reference.startswith("E06:"):
                _require(
                    reference[4:] in e06_oracle_ids,
                    "Unknown E06 oracle case reference.",
                )
            else:
                raise ValueError(f"Unknown oracle reference {reference}.")
    for scenario in scenarios:
        projected = project_case_inputs(scenario["id"])
        _require(bool(projected), f"Scenario {scenario['id']} has no case input.")

    return {
        "schema": catalog["schema"],
        "version": catalog["version"],
        "scenario_count": len(scenarios),
        "recovery_count": len(catalog["scenario_sets"]["recovery"]),
        "pending_imagepull_count": len(catalog["scenario_sets"]["pending_imagepull"]),
        "negative_count": len(catalog["scenario_sets"]["negative"]),
        "oracle_sha256": oracle_sha,
        "input_sha256": sources["e10_s3_inputs"]["sha256"],
        "controls_sha256": sources["e10_s3_controls"]["sha256"],
        "labels_in_case_inputs": False,
    }


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_post_check_attestation(post_check, receipt):
    required = {
        "schema",
        "status",
        "trial_id",
        "run_id",
        "window_id",
        "target_uid",
        "execution_id",
        "verification_profile_digest",
        "execution_receipt_sha256",
        "verification_receipt_sha256",
        "fault_observation_sha256",
        "fault_treatment_sha256",
        "action_state",
        "load_started_at",
        "verification_acknowledged_at",
        "verification_deadline",
        "verification_finalized_at",
        "verification_window_seconds",
        "expected_rollout_samples",
        "valid_rollout_samples",
        "post_check_observed_at",
        "offered_sample_at",
        "load_stopped_at",
        "load_final_state",
        "requested_rate_rps",
        "observed_rate_rps",
        "offered_window_rate_rps",
    }
    _require(
        isinstance(post_check, dict) and set(post_check) == required,
        "Recovery post-check attestation is incomplete or has unexpected fields.",
    )
    _require(
        post_check["schema"] == "E10RecoveryPostCheck/v1"
        and post_check["status"] == "passed",
        "Recovery post-check did not pass.",
    )
    for field in ("trial_id", "run_id", "window_id"):
        _require(
            post_check[field] == receipt[field], f"Post-check {field} binding changed."
        )
    _require(
        post_check["target_uid"] == receipt["target_uid"],
        "Post-check target UID binding changed.",
    )
    for field in (
        "verification_profile_digest",
        "execution_receipt_sha256",
        "verification_receipt_sha256",
        "fault_observation_sha256",
        "fault_treatment_sha256",
    ):
        _require(
            isinstance(post_check[field], str)
            and re.fullmatch(r"[a-f0-9]{64}", post_check[field]) is not None,
            f"Post-check {field} is not a SHA-256 digest.",
        )
    try:
        UUID(post_check["execution_id"])
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Post-check execution identity must be a UUID.") from None
    scenario = {item["id"]: item for item in load_catalog()["scenarios"]}[
        receipt["scenario_id"]
    ]
    _require(
        scenario["kind"] == "recovery"
        and post_check["verification_profile_digest"]
        == receipt["declared_resources"].get("qualification_profile_digest"),
        "Post-check is not bound to the qualified recovery profile.",
    )
    qualified = receipt["declared_resources"].get("qualified_action")
    _require(
        isinstance(qualified, dict) and post_check["action_state"] == qualified,
        "Post-check action state differs from the qualified recovery state.",
    )
    evidence = receipt.get("evidence", {})
    fault = evidence.get("fault") if isinstance(evidence, dict) else None
    _require(
        isinstance(fault, dict)
        and fault.get("observation_sha256") == post_check["fault_observation_sha256"]
        and _canonical_sha256(fault.get("expected"))
        == post_check["fault_treatment_sha256"],
        "Post-check fault treatment is not bound to the observed injection.",
    )
    fault_at = _parse_timestamp(fault.get("observed_at"), "fault observed_at")
    for field in (
        "verification_window_seconds",
        "expected_rollout_samples",
        "valid_rollout_samples",
        "requested_rate_rps",
    ):
        _require(
            type(post_check[field]) is int and post_check[field] > 0,
            f"Post-check {field} is invalid.",
        )
    _require(
        post_check["verification_window_seconds"] in {180, 300}
        and post_check["expected_rollout_samples"]
        == post_check["valid_rollout_samples"],
        "Post-check sample coverage is incomplete.",
    )
    _require(
        post_check["expected_rollout_samples"]
        == (post_check["verification_window_seconds"] - 60) // 30 + 1
        and post_check["requested_rate_rps"] <= 30,
        "Post-check sample count or offered-rate bound is invalid.",
    )
    for field in ("observed_rate_rps", "offered_window_rate_rps"):
        _require(
            isinstance(post_check[field], (int, float))
            and not isinstance(post_check[field], bool)
            and math.isfinite(post_check[field])
            and post_check["requested_rate_rps"] * 0.8
            <= post_check[field]
            <= post_check["requested_rate_rps"] * 1.2,
            f"Post-check {field} differs from the fixed offered rate.",
        )
    load = evidence.get("offered_load") if isinstance(evidence, dict) else None
    _require(
        isinstance(load, dict)
        and load.get("state") == post_check["load_final_state"]
        and post_check["load_final_state"] in {"complete", "stopped_after_post_check"},
        "Recovery offered load did not remain active through its positive post-check.",
    )
    load_started_at = _parse_timestamp(post_check["load_started_at"], "load_started_at")
    load_stopped_at = _parse_timestamp(
        load.get("finished_at"), "offered load finished_at"
    )
    acknowledged_at = _parse_timestamp(
        post_check["verification_acknowledged_at"], "verification_acknowledged_at"
    )
    deadline = _parse_timestamp(
        post_check["verification_deadline"], "verification_deadline"
    )
    finalized_at = _parse_timestamp(
        post_check["verification_finalized_at"], "verification_finalized_at"
    )
    observed_at = _parse_timestamp(
        post_check["post_check_observed_at"], "post_check_observed_at"
    )
    sample_at = _parse_timestamp(post_check["offered_sample_at"], "offered_sample_at")
    stopped_at = _parse_timestamp(post_check["load_stopped_at"], "load_stopped_at")
    _require(
        load.get("started_at") == post_check["load_started_at"]
        and type(load.get("rate")) is int
        and load.get("rate") == post_check["requested_rate_rps"]
        and type(load.get("seconds")) is int
        and load.get("finished_at") == post_check["load_stopped_at"]
        and load_stopped_at == stopped_at
        and load_started_at + timedelta(seconds=load["seconds"]) >= deadline
        and isinstance(load.get("observations"), list)
        and any(
            isinstance(observation, dict)
            and observation.get("phase") == "positive-post-check"
            and observation.get("deployment_uid") == post_check["target_uid"]
            and observation.get("expected_rate_rps") == post_check["requested_rate_rps"]
            and observation.get("observed_rate_rps") == post_check["observed_rate_rps"]
            and abs(float(observation.get("sample_at", "nan")) - sample_at.timestamp())
            <= 1
            for observation in load["observations"]
        )
        and load_started_at <= fault_at <= acknowledged_at
        and load_started_at
        <= acknowledged_at
        < deadline
        <= finalized_at
        <= observed_at
        < stopped_at
        and deadline <= sample_at <= observed_at
        and (observed_at - sample_at).total_seconds() <= 120
        and (deadline - acknowledged_at).total_seconds()
        == post_check["verification_window_seconds"],
        "Offered load did not span the entire positive post-check window.",
    )


def validate_trial_receipt(value):
    catalog = load_catalog()
    schema = read_json(RECEIPT_SCHEMA)
    _require(isinstance(value, dict), "Trial reset receipt must be an object.")
    receipt = value.get("trial_receipt", value)
    _require(isinstance(receipt, dict), "Trial reset receipt must be an object.")
    required = set(schema["required"])
    _require(
        required <= set(receipt), "Trial reset receipt is missing required fields."
    )
    _require(
        set(receipt) <= set(schema["properties"]),
        "Trial reset receipt has unexpected fields.",
    )
    _require(
        receipt["schema"] == "E10TrialResetReceipt/v1",
        "Unknown trial reset receipt version.",
    )
    try:
        ids = [
            UUID(receipt[name])
            for name in ("trial_id", "run_id", "window_id", "reset_id")
        ]
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Trial/run/window/reset IDs must be UUIDs.") from None
    _require(
        len(set(ids)) == len(ids),
        "Trial/run/window/reset IDs must be independently minted.",
    )
    scenarios = {item["id"]: item for item in catalog["scenarios"]}
    _require(
        receipt["scenario_id"] in scenarios, "Unknown scenario ID in reset receipt."
    )
    _require(
        receipt["reset_state"] in {"healthy", "interrupted", "faulted"},
        "Unknown reset origin.",
    )
    _require(
        receipt["receipt_status"] in {"reset_complete", "manual", "incomplete"},
        "Unknown reset status.",
    )
    scope = receipt.get("scope")
    _require(isinstance(scope, dict), "Reset receipt scope must be an object.")
    for digest in (
        receipt["source_binding_sha256"],
        scope.get("scope_manifest_sha256"),
    ):
        _require(
            isinstance(digest, str)
            and re.fullmatch(r"[a-f0-9]{64}", digest) is not None,
            "Invalid scope/source digest.",
        )
    _require(
        scope.get("project") == "cloudops-local-testing-lab",
        "Reset receipt scope is not the owned lab.",
    )
    _require(
        set(scope) == {"project", "namespace", "scope_manifest_sha256"},
        "Reset receipt scope has unexpected fields.",
    )
    _require(
        scope.get("namespace") == "demo",
        "Reset receipt namespace is not scoped.",
    )
    _require(
        isinstance(receipt["target_uid"], str) and bool(receipt["target_uid"]),
        "Reset receipt target UID is missing.",
    )
    _require(
        isinstance(receipt["image_digests"], list)
        and bool(receipt["image_digests"])
        and all(isinstance(item, str) and item for item in receipt["image_digests"]),
        "Reset receipt runtime image IDs are missing.",
    )
    required_demand = _validate_declared_resources(receipt["declared_resources"])
    headroom = receipt["surge_headroom"]
    _require(
        isinstance(headroom, dict), "Reset receipt surge headroom must be an object."
    )
    _require(
        set(headroom)
        == {
            "required_cpu_millicores",
            "available_cpu_millicores",
            "required_memory_bytes",
            "available_memory_bytes",
            "qualified",
        },
        "Reset receipt surge headroom has unexpected fields.",
    )
    for field in (
        "required_cpu_millicores",
        "available_cpu_millicores",
        "required_memory_bytes",
        "available_memory_bytes",
    ):
        _require(
            type(headroom[field]) is int and headroom[field] >= 0,
            "Invalid surge headroom value.",
        )
    _require(
        headroom["required_cpu_millicores"]
        == required_demand["required_cpu_millicores"]
        and headroom["required_memory_bytes"]
        == required_demand["required_memory_bytes"],
        "Reset receipt surge demand does not match its declared baseline/qualified resources.",
    )
    _require(
        type(headroom["qualified"]) is bool
        and headroom["qualified"]
        == (
            headroom["available_cpu_millicores"] >= headroom["required_cpu_millicores"]
            and headroom["available_memory_bytes"] >= headroom["required_memory_bytes"]
        ),
        "Surge qualification does not match the measured headroom.",
    )
    for field in ("baseline_at", "observed_at"):
        try:
            stamp = datetime.fromisoformat(receipt[field].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid receipt timestamp {field}.") from None
        _require(
            stamp.tzinfo is not None,
            f"Receipt timestamp {field} must be timezone-aware.",
        )
    source_limits = receipt["source_limits"]
    _require(
        isinstance(source_limits, dict),
        "Reset receipt source limits must be an object.",
    )
    _require(
        set(source_limits)
        == {"request_timeout_seconds", "response_bytes", "series_limit"},
        "Reset receipt source limits have unexpected fields.",
    )
    _require(
        isinstance(source_limits["request_timeout_seconds"], (int, float))
        and source_limits["request_timeout_seconds"] > 0
        and type(source_limits["response_bytes"]) is int
        and source_limits["response_bytes"] > 0
        and type(source_limits["series_limit"]) is int
        and source_limits["series_limit"] > 0,
        "Invalid source limits.",
    )
    for field in ("residual_eligible_actions", "duplicate_targeted_loads"):
        count = receipt[field]
        _require(
            count is None or (type(count) is int and count >= 0),
            f"Invalid count {field}.",
        )
    _require(isinstance(receipt["phases"], dict), "Receipt phases must be an object.")
    for phase, phase_at in receipt["phases"].items():
        if phase_at is not None:
            try:
                phase_stamp = datetime.fromisoformat(phase_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                raise ValueError(f"Invalid phase timestamp {phase}.") from None
            _require(
                phase_stamp.tzinfo is not None,
                f"Phase timestamp {phase} must be timezone-aware.",
            )
    _require(
        receipt.get("incomplete_reason") is None
        or isinstance(receipt["incomplete_reason"], str),
        "Incomplete reason must be text or null.",
    )
    if receipt["receipt_status"] == "reset_complete":
        _require(
            headroom["available_cpu_millicores"] >= headroom["required_cpu_millicores"]
            and headroom["available_memory_bytes"] >= headroom["required_memory_bytes"],
            "Reset receipt does not prove declared surge demand fits measured headroom.",
        )
        _require(
            receipt["residual_eligible_actions"] == 0,
            "Eligible actions remain after a completed reset.",
        )
        _require(
            receipt["duplicate_targeted_loads"] == 0,
            "Duplicate targeted load remains after a completed reset.",
        )
        _require(
            headroom.get("qualified") is True,
            "Completed reset lacks surge qualification.",
        )
        if scenarios[receipt["scenario_id"]]["kind"] == "recovery":
            _validate_post_check_attestation(receipt.get("post_check"), receipt)
    else:
        _require(
            bool(receipt.get("incomplete_reason")),
            "Manual/incomplete receipt needs a reason.",
        )
    return receipt


def validate_qualification_receipt(value):
    catalog = load_catalog()
    schema = read_json(ROOT / catalog["qualification_receipt_schema"])
    _require(isinstance(value, dict), "Qualification receipt must be an object.")
    required = set(schema["required"])
    _require(
        required <= set(value), "Qualification receipt is missing required fields."
    )
    _require(
        set(value) <= set(schema["properties"]),
        "Qualification receipt has unexpected fields.",
    )
    _require(
        value["schema"] == "E10RecoveryQualificationReceipt/v1",
        "Unknown recovery qualification receipt version.",
    )
    recovery_scenarios = set(catalog["scenario_sets"]["recovery"])
    _require(
        value["scenario_id"] in recovery_scenarios,
        "Qualification receipt must identify a catalog recovery scenario.",
    )
    _require(value["qualified"] is True, "Unqualified target remains manual.")
    _require(bool(value["target_uid"]), "Qualification target UID is missing.")
    for name in ("source_binding_sha256", "profile_digest"):
        _require(
            re.fullmatch(r"[a-f0-9]{64}", value[name]) is not None,
            f"Invalid qualification digest {name}.",
        )
    _require(
        isinstance(value["image_digests"], list)
        and bool(value["image_digests"])
        and all(isinstance(item, str) and item for item in value["image_digests"]),
        "Qualified runtime image IDs are missing.",
    )
    declared = value["declared_resources"]
    _require(
        isinstance(declared, dict)
        and set(declared) == {"replicas", "template", "rolling_update"},
        "Qualified desired resource state must include exact replicas, template and rollout settings.",
    )
    _require(
        type(declared["replicas"]) is int and declared["replicas"] >= 1,
        "Qualified desired replica count is invalid.",
    )
    template = declared["template"]
    _require(
        isinstance(template, dict)
        and set(template)
        == {"image", "resources", "imagePullPolicy", "mode", "initContainers"},
        "Qualified action template must describe every reset-controlled field.",
    )
    _require(
        isinstance(template["image"], str)
        and bool(template["image"])
        and isinstance(template["resources"], dict)
        and isinstance(template["imagePullPolicy"], str)
        and bool(template["imagePullPolicy"]),
        "Qualified action image or resources are invalid.",
    )
    _require(
        template["mode"] is None
        or (
            isinstance(template["mode"], dict)
            and set(template["mode"]) == {"name", "value"}
            and template["mode"]["name"] == "LAB_MODE"
            and isinstance(template["mode"]["value"], str)
        ),
        "Qualified action mode is invalid.",
    )
    _require(
        template["initContainers"] is None
        or isinstance(template["initContainers"], list),
        "Qualified init-container state is invalid.",
    )
    _require(
        isinstance(declared["rolling_update"], dict),
        "Qualified rollout strategy must be an object.",
    )
    headroom = value["surge_headroom"]
    _require(isinstance(headroom, dict), "Qualified surge headroom must be an object.")
    _require(
        set(headroom) == {"required_cpu_millicores", "required_memory_bytes"},
        "Qualified surge headroom has unexpected fields.",
    )
    for field in ("required_cpu_millicores", "required_memory_bytes"):
        _require(
            type(headroom[field]) is int and headroom[field] >= 0,
            "Invalid qualified surge demand.",
        )
    _require(
        not (
            {"authorization", "password", "private_key", "secret", "token"}
            & set(_walk_keys(value))
        ),
        "Qualification receipt must not contain credentials.",
    )
    return value


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["validate", "project", "validate-receipt", "validate-qualification"],
    )
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.operation == "validate":
        print(json.dumps(validate_catalog(), indent=2, sort_keys=True))
    elif args.value is None:
        parser.error(f"{args.operation} requires a JSON path")
    elif args.operation == "project":
        print(json.dumps(project_case_inputs(args.value), indent=2, sort_keys=True))
    elif args.operation == "validate-qualification":
        qualification = validate_qualification_receipt(read_json(Path(args.value)))
        print(
            json.dumps(
                {
                    "schema": qualification["schema"],
                    "scenario_id": qualification["scenario_id"],
                    "target_uid": qualification["target_uid"],
                    "profile_digest": qualification["profile_digest"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        receipt = validate_trial_receipt(read_json(Path(args.value)))
        print(
            json.dumps(
                {
                    "schema": receipt["schema"],
                    "scenario_id": receipt["scenario_id"],
                    "receipt_status": receipt["receipt_status"],
                    "trial_id": receipt["trial_id"],
                    "reset_id": receipt["reset_id"],
                },
                indent=2,
                sort_keys=True,
            )
        )
