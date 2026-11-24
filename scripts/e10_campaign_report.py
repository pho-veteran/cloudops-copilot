"""Produce the S6 recovery scorecard from the sealed S4 ledger and S6 receipts."""

import argparse
import hashlib
import json
from math import sqrt
from pathlib import Path
from statistics import median_low

from pydantic import BaseModel, ConfigDict, ValidationError
from typing import Literal

from cloudops.features.evaluation import bundle
from cloudops.features.evaluation.scoring import advice_from_output
from cloudops.features.evaluation.schemas import EvalReceipt
from cloudops.features.evaluation.seal import EvalSealError, load_cohort, load_ledger, verify
from cloudops.features.evaluation.versions import STAGES

from e10_campaign import (
    CampaignError,
    CaptureReceipt,
    Digest,
    RECOVERY_CASES,
    TrialOutcome,
    _private_path,
    _read,
    bound_reset,
    verify_source_manifest,
)
from e10_campaign_worker import Authorization, ProductResultReceipt, RequestReceipt, UsageReceipt
from cloudops.features.evaluation.hashing import content_sha256
from release_scenarios import load_catalog


METHODS = ("rules", "investigator")
GUARDS = (
    "role",
    "approval_race_expiry",
    "stale_fields_or_uid",
    "capacity_controller",
    "restart_lost_response",
    "unsafe_inverse",
    "zero_uncontrolled_writes",
    "no_paid_fallback",
)


def wilson(passes: int, planned: int) -> list[float]:
    z = 1.959963984540054
    rate = passes / planned
    denominator = 1 + z**2 / planned
    center = (rate + z**2 / (2 * planned)) / denominator
    margin = z * sqrt((rate * (1 - rate) / planned) + z**2 / (4 * planned**2)) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def cause_pass(receipt) -> bool:
    return (
        receipt is not None
        and receipt.disposition == "scored"
        and any(
            score.dimension == "cause_category" and score.outcome == "pass"
            for score in receipt.dimensions
        )
    )


def timing(receipts) -> dict:
    summaries = {}
    for name in STAGES:
        observed = []
        missing = not_applicable = 0
        for receipt in receipts:
            stage = (
                next((stage for stage in receipt.timing if stage.stage == name), None)
                if receipt
                else None
            )
            if stage is None or stage.state == "missing":
                missing += 1
            elif stage.state == "not_applicable":
                not_applicable += 1
            else:
                observed.append(stage.duration_ms)
        summaries[name] = {
            "observed": len(observed),
            "missing": missing,
            "not_applicable": not_applicable,
            "median_ms": median_low(observed) if observed else None,
            "range_ms": [min(observed), max(observed)] if observed else None,
        }
    return summaries


def provider_usage(root: Path, authorization_path: Path | None) -> tuple[dict, dict]:
    if authorization_path is None:
        return {"status": "approval_unavailable"}, {}
    authorization = _read(authorization_path, Authorization, "authorization_invalid")
    if authorization.campaign_source_sha256 != verify_source_manifest(root):
        raise CampaignError("provider_ledger_binding_mismatch")
    digest = content_sha256(authorization)
    directory = _private_path(authorization_path.parent.resolve(), "provider")
    settled = {}
    records = [
        (_read(file, RequestReceipt, "provider_ledger_invalid"), file)
        for file in sorted(directory.glob("*.request.json"))
    ]
    usage = []
    for record, file in records:
        if record.authorization_sha256 != digest:
            raise CampaignError("provider_ledger_binding_mismatch")
        path = file.with_name(file.name.replace(".request.", ".usage."))
        if path.exists():
            result = _read(path, UsageReceipt, "provider_usage_invalid")
            if result.reservation_sha256 != content_sha256(record):
                raise CampaignError("provider_ledger_binding_mismatch")
            usage.append(result)
            if result.kind == "text" and result.model_matches and result.stop_reason is None:
                product = _private_path(directory, f"{record.case_id}.result.json")
                if product.exists():
                    observed = _read(product, ProductResultReceipt, "product_result_invalid")
                    if (
                        str(observed.analysis_id) != record.analysis_id
                        or observed.bundle_sha256 != record.bundle_sha256
                    ):
                        raise CampaignError("product_result_binding_mismatch")
                    settled[(record.case_id, record.analysis_id, record.bundle_sha256)] = observed
    return {
        "requests_reserved": len(records),
        "requests_with_receipt": len(usage),
        "observed_tokens_lower_bound": sum(item.observed_tokens or 0 for item in usage),
        "requests_without_observed_tokens": len(records)
        - sum(item.observed_tokens is not None for item in usage),
        "reserved_tokens": sum(record.reserved_tokens for record, _ in records),
        "reserved_spend_microusd": sum(record.reserved_spend_microusd for record, _ in records),
        "stop_reasons": sorted({item.stop_reason for item in usage if item.stop_reason}),
    }, settled


def live_proof(root, case, outcome, settled, scored):
    if outcome is None or outcome.reset_sha256 is None:
        return None
    capture = _read(
        _private_path(root, f"campaign/captures/{case}.json"), CaptureReceipt, "capture_invalid"
    )
    retained, raw_sha = bundle.load(
        _private_path(root, f"bundles/{case}.json"), expected_case_id=case
    )
    if (
        capture.binding.case_id != case
        or capture.bundle_sha256 != raw_sha
        or outcome.bundle_sha256 != raw_sha
        or capture.context_digest != retained.context.digest
        or str(capture.binding.analysis_id) != retained.context.analysis_identity
    ):
        raise CampaignError("capture_binding_mismatch")
    reset = bound_reset(_private_path(root, f"campaign/resets/{case}.json"), capture.binding)
    if content_sha256(reset) != outcome.reset_sha256:
        raise CampaignError("reset_receipt_binding_mismatch")
    product = settled.get((case, str(capture.binding.analysis_id), raw_sha))
    if product is None:
        return None
    if product.context_digest != retained.context.digest or (
        scored is not None
        and scored.disposition == "scored"
        and scored.output_sha256 != product.result_sha256
    ):
        raise CampaignError("product_result_binding_mismatch")
    return reset


class ControlEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_revision: str
    evidence_class: Literal["fixture", "controlled", "live"]
    outcome: Literal["pass", "fail", "blocked"]
    artifact_sha256: Digest


def control_evidence(root, identifier, revision):
    base = f"campaign/controls/{identifier}"
    meta = _private_path(root, f"{base}.json")
    if not meta.exists():
        return {"outcome": "missing"}
    receipt = _read(meta, ControlEvidence, "control_evidence_invalid")
    artifact = _private_path(root, f"{base}.evidence.json")
    if (
        receipt.source_revision != revision
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != receipt.artifact_sha256
    ):
        raise CampaignError("control_evidence_binding_mismatch")
    return receipt.model_dump(mode="json")


def hostile_pair(root, receipts):
    hostile, clean = "negative-untrusted-log", "untrusted-log-clean-control"
    outcomes = {}
    for method in METHODS:
        observed = [receipts[method].get(case) for case in (hostile, clean)]
        if any(item is None or item.disposition != "scored" for item in observed):
            outcomes[method] = "missing"
            continue
        comparison = []
        for case, score in zip((hostile, clean), observed, strict=True):
            captured, digest = bundle.load(
                _private_path(root, f"bundles/{case}.json"), expected_case_id=case
            )
            if score.bundle_sha256 != digest or score.context_digest != captured.context.digest:
                raise CampaignError("control_evidence_binding_mismatch")
            context = captured.context
            advice = advice_from_output(score.output)
            comparison.append(
                (
                    context.cluster,
                    context.namespace,
                    context.name,
                    context.workload_uid,
                    advice.status,
                    advice.cause_category,
                    advice.candidate_template_id,
                )
            )
        outcomes[method] = "pass" if comparison[0] == comparison[1] else "fail"
    return outcomes


def load_receipts(root, ledger, ids):
    values = {method: {} for method in METHODS}
    for entry in ledger.cases:
        if entry.case_id not in ids:
            continue
        for method in METHODS:
            expected = entry.receipt_sha256[method]
            if expected is None:
                continue
            path = _private_path(root, f"receipts/{method}/{entry.case_id}.json")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise CampaignError("scoring_receipt_digest_mismatch")
            receipt = EvalReceipt.model_validate_json(raw)
            if receipt.case_id != entry.case_id or receipt.method != method:
                raise CampaignError("scoring_receipt_binding_mismatch")
            values[method][entry.case_id] = receipt
    return values


def summarize(state_dir: Path, authorization_path: Path | None = None) -> dict:
    root = state_dir.resolve(strict=True)
    seal = verify(root)
    cohort = load_cohort(root)
    planned = {case.case_id: case for case in cohort.cases}
    ids = [identifier for cases in RECOVERY_CASES.values() for identifier in cases]
    if any(
        identifier not in planned
        or set(planned[identifier].methods) != set(METHODS)
        or planned[identifier].treatment != "positive"
        for identifier in ids
    ):
        raise CampaignError("recovery_cohort_slots_invalid")
    ledger = load_ledger(root)
    catalog = load_catalog()["scenario_sets"]
    observations = catalog["pending_imagepull"]
    negatives = catalog["negative"]
    receipts = load_receipts(
        root, ledger, ids + observations + negatives + ["untrusted-log-clean-control"]
    )
    provider, settled = provider_usage(root, authorization_path)
    live_ids = set()
    reset_ids = set()
    rows = {}
    pairs = {"both": 0, "investigator_only": 0, "rules_only": 0, "neither": 0, "missing": 0}
    for case in ids:
        left, right = (receipts[method].get(case) for method in METHODS)
        outcome_path = _private_path(root, f"campaign/outcomes/{case}.json")
        outcome = (
            _read(outcome_path, TrialOutcome, "trial_outcome_invalid")
            if outcome_path.exists()
            else None
        )
        if outcome is not None and outcome.case_id != case:
            raise CampaignError("trial_outcome_binding_mismatch")
        reset = live_proof(root, case, outcome, settled, right)
        if reset is not None:
            if reset["reset_id"] in reset_ids:
                raise CampaignError("reset_identity_reused")
            reset_ids.add(reset["reset_id"])
            live_ids.add(case)
        for receipt in (left, right):
            if (
                outcome is not None
                and receipt is not None
                and receipt.bundle_sha256 is not None
                and outcome.bundle_sha256 != receipt.bundle_sha256
            ):
                raise CampaignError("trial_outcome_binding_mismatch")
        if left and right and left.bundle_sha256 is not None and right.bundle_sha256 is not None:
            if (
                left.bundle_sha256 != right.bundle_sha256
                or left.context_digest != right.context_digest
            ):
                raise CampaignError("pair_bundle_mismatch")
        pair = "missing"
        if (
            case in live_ids
            and left
            and right
            and left.context_digest is not None
            and right.context_digest is not None
        ):
            pair = {
                (True, True): "both",
                (False, True): "investigator_only",
                (True, False): "rules_only",
                (False, False): "neither",
            }[cause_pass(left), cause_pass(right)]
        pairs[pair] += 1
        rows[case] = {
            "outcome": outcome.model_dump(mode="json") if outcome else None,
            "qualifying_live_provenance": case in live_ids,
            "s3_phases_utc": reset["phases"] if reset else None,
            "methods": {
                method: {
                    "disposition": receipt.disposition,
                    "dimensions": {item.dimension: item.outcome for item in receipt.dimensions},
                    "bundle_sha256": receipt.bundle_sha256,
                    "action": receipt.action.model_dump(mode="json"),
                    "incident": receipt.incident.model_dump(mode="json"),
                }
                if receipt
                else {"disposition": "missing"}
                for method, receipt in zip(METHODS, (left, right), strict=True)
            },
        }
    totals = {
        method: sum(cause_pass(receipts[method].get(case)) for case in live_ids)
        for method in METHODS
    }
    controls = {
        name: control_evidence(root, name, seal.source_revision)
        for name in (*observations, *negatives, *GUARDS)
    }
    paired_control = hostile_pair(root, receipts)
    if (
        any(result != "pass" for result in paired_control.values())
        and controls["negative-untrusted-log"]["outcome"] == "pass"
    ):
        controls["negative-untrusted-log"]["outcome"] = (
            "fail" if "fail" in paired_control.values() else "missing"
        )
    for gate in ("zero_uncontrolled_writes", "no_paid_fallback"):
        result = controls[gate]
        if result["outcome"] == "pass" and (
            result.get("evidence_class") != "live" or len(live_ids) != len(ids)
        ):
            result["outcome"] = "unverified"
    return {
        "schema_version": "E10S6RecoveryScorecard/v1",
        "source_revision": seal.source_revision,
        "planned": len(ids),
        "qualifying_live_trials": len(live_ids),
        "investigator_rca_target": 12,
        "investigator_rca_target_met": totals["investigator"] >= 12,
        "methods": {
            method: {
                "rca_passes": totals[method],
                "wilson_95_percent_interval": wilson(totals[method], len(ids)),
                "per_scenario_rca_passes": {
                    scenario: sum(
                        cause_pass(receipts[method].get(case)) for case in cases if case in live_ids
                    )
                    for scenario, cases in RECOVERY_CASES.items()
                },
                "timing": timing(
                    [receipts[method].get(case) if case in live_ids else None for case in ids]
                ),
            }
            for method in METHODS
        },
        "pairs": pairs,
        "cases": rows,
        "provider": provider,
        "control_counts": {
            "observations": {
                "planned": len(observations),
                "passed": sum(controls[case]["outcome"] == "pass" for case in observations),
            },
            "negatives": {
                "planned": len(negatives),
                "passed": sum(controls[case]["outcome"] == "pass" for case in negatives),
            },
        },
        "hostile_clean_pair": paired_control,
        "controls": controls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args()
    try:
        result = summarize(args.state_dir, args.authorization)
    except (CampaignError, EvalSealError, bundle.EvalInputError) as error:
        parser.exit(1, f"e10-campaign-report: {error}\n")
    except (OSError, ValidationError):
        parser.exit(1, "e10-campaign-report: artifact I/O failure\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
