"""Bind the S6 observation and negative cases to controlled-fixture product results.

The release catalog (config/lab/release-scenarios-v1.json) classifies the two Pending
and two ImagePull variants and the five named negatives as controlled fixtures, not
live recovery trials. They run once through the installed product path with the E06
campaign driver at the sealed revision, with ``capture_contexts`` enabled:

- ``import-fixtures`` publishes each captured prepared context as the planned S6 bundle
  and the product's validated result as the Investigator method output, before scoring.
- ``record-controls`` derives each control outcome from the scored S4 receipts, the
  driver ledger or a focused-test JUnit report, and retains the artifact it used.

No outcome is accepted from the operator. A binding mismatch stops the command instead
of being recorded as a failure.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal
from uuid import UUID
import xml.etree.ElementTree as ElementTree

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from cloudops.features.diagnosis.context import PreparedContext, recomputed_digest
from cloudops.features.evaluation import bundle
from cloudops.features.evaluation.hashing import content_sha256
from cloudops.features.evaluation.seal import EvalSealError, load_ledger, verify

from e10_campaign import (
    ROOT,
    CampaignError,
    CaseId,
    Digest,
    _case,
    _private_path,
    _private_state,
    _read,
    _write_once,
    read_json,
    verify_source_manifest,
)
from e10_campaign_report import ControlEvidence, load_receipts
from release_scenarios import load_catalog

CLEAN_CONTROL = "untrusted-log-clean-control"
HOSTILE = "negative-untrusted-log"
# S6 case -> E06 driver case, matching each scenario's catalog input reference.
FIXTURE_SOURCES = {
    "pending-insufficient-cpu": "c07",
    "pending-untolerated-taint": "c08",
    "imagepull-missing-manifest": "c09",
    "imagepull-unauthorized": "c10",
    "negative-healthy": "n-healthy",
    HOSTILE: "n-injection",
    CLEAN_CONTROL: "c01",
}
INSUFFICIENT = "negative-insufficient-evidence"
INSUFFICIENT_SOURCE = "n-required-gap"
OUT_OF_SCOPE = "negative-out-of-scope"
QUOTA = "negative-provider-unavailable-quota"
QUOTA_MODULE = "test_diagnosis_lifecycle"
QUOTA_TESTS = (
    "test_unavailable_zero_network[unqualified]",
    "test_unavailable_zero_network[disabled]",
    "test_unavailable_zero_network[unconfigured]",
    "test_quota_hold_resume_keeps_budgets_and_deadline",
)
# Dimensions a control needs; evidence-reference quality is reported by the scorer only.
REQUIRED_DIMENSIONS = {
    "observation": ("cause_category", "abstention_manual", "eligible_plan", "action_outcome"),
    "negative-healthy": ("cause_category", "abstention_manual", "eligible_plan", "action_outcome"),
    HOSTILE: ("eligible_plan", "action_outcome"),
}
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_JUNIT_BYTES = 2 * 1024 * 1024
E06_CASES = "services/backend/tests/fixtures/e06/cases.json"
DRIVER_DIRECTORY = ROOT / "services/backend/tests"


def driver_source_digest() -> str:
    """The E06 driver's own digest of the backend and driver files in this checkout."""
    if str(DRIVER_DIRECTORY) not in sys.path:
        sys.path.insert(0, str(DRIVER_DIRECTORY))
    import e06_live_acceptance

    return e06_live_acceptance.source_tree_digest()


class FixtureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["E10S6FixtureReceipt/v1"] = "E10S6FixtureReceipt/v1"
    case_id: CaseId
    source_case: str
    source_revision: str
    ledger_sha256: Digest
    behavior_digest: Digest
    analysis_id: UUID
    bundle_sha256: Digest
    context_digest: Digest
    result_sha256: Digest


def _scenario(case_id: str) -> str:
    return HOSTILE if case_id == CLEAN_CONTROL else case_id


def check_catalog() -> None:
    """The fixed mapping must match the catalog's own input references."""
    catalog = load_catalog()
    scenarios = {item["id"]: item for item in catalog["scenarios"]}
    sets = catalog["scenario_sets"]
    if set(FIXTURE_SOURCES) - {CLEAN_CONTROL} | {INSUFFICIENT, OUT_OF_SCOPE, QUOTA} != set(
        sets["pending_imagepull"]
    ) | set(sets["negative"]):
        raise CampaignError("fixture_mapping_incomplete")
    for case_id, source in FIXTURE_SOURCES.items():
        if f"E06:{source}" not in scenarios[_scenario(case_id)]["input_refs"]:
            raise CampaignError("fixture_mapping_mismatch")
    inputs = read_json(ROOT / catalog["source_fixtures"]["e10_s3_inputs"]["path"])
    insufficient = next(case for case in inputs["cases"] if case["id"] == INSUFFICIENT)
    if (
        f"E10S3:{INSUFFICIENT}" not in scenarios[INSUFFICIENT]["input_refs"]
        or insufficient["evidence"] != {}
    ):
        raise CampaignError("fixture_mapping_mismatch")


def fixture_ledger(fixture_dir: Path, source_revision: str, behavior_digest: str):
    path = fixture_dir / "ledger.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LEDGER_BYTES:
            raise CampaignError("fixture_ledger_invalid")
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        raise CampaignError("fixture_ledger_invalid") from None
    cases_sha = hashlib.sha256((ROOT / E06_CASES).read_bytes()).hexdigest()
    identity = data.get("primary_identity") if isinstance(data, dict) else None
    if (
        not isinstance(identity, dict)
        or data.get("evaluation_finished") is not True
        or data.get("source_commit") != source_revision
        or data.get("cases_sha256") != cases_sha
        or data.get("source_tree_sha256") != driver_source_digest()
        or identity.get("behavior_digest") != behavior_digest
        or not isinstance(data.get("cases"), dict)
        or not isinstance(data.get("calls"), list)
    ):
        raise CampaignError("fixture_ledger_binding_mismatch")
    return data, hashlib.sha256(raw).hexdigest()


def _publish(path: Path, value: BaseModel, code: str) -> None:
    """Write once; an identical earlier write (an interrupted import) is accepted."""
    if path.exists():
        expected = (value.model_dump_json() + "\n").encode("utf-8")
        if path.read_bytes() != expected:
            raise CampaignError(code)
        return
    _write_once(path, value)


def import_fixtures(state_dir: Path, fixture_dir: Path, behavior_digest: str) -> list[dict]:
    state = _private_state(state_dir)
    seal = verify(state, include_scoring_inputs=False)
    verify_source_manifest(state)
    check_catalog()
    fixture_dir = _private_state(fixture_dir)
    data, ledger_sha = fixture_ledger(fixture_dir, seal.source_revision, behavior_digest)
    published = []
    for case_id, source in FIXTURE_SOURCES.items():
        entry = data["cases"].get(source)
        result = entry.get("result") if isinstance(entry, dict) else None
        if (
            not isinstance(result, dict)
            or entry.get("identity") != data["primary_identity"]
            or entry.get("purpose") != "evaluation"
            or entry.get("state") not in {"completed", "insufficient_evidence"}
            or not isinstance(entry.get("calls"), int)
            or entry["calls"] < 1
        ):
            raise CampaignError("fixture_result_unavailable")
        context = _read(
            _private_path(fixture_dir, f"contexts/{source}.json"),
            PreparedContext,
            "fixture_context_invalid",
        )
        if (
            recomputed_digest(context) != context.digest
            or context.digest != result.get("context_digest")
            or context.analysis_identity != entry.get("analysis_id")
            or str(context.run_id) != entry.get("run_id")
            or result.get("run_id") != entry.get("run_id")
        ):
            raise CampaignError("fixture_context_binding_mismatch")
        case = _case(state, case_id)
        bundle_path = _private_path(state, case.bundle)
        if bundle_path.exists():
            retained, _ = bundle.load(bundle_path, expected_case_id=case_id)
            if retained.context != context or retained.bundle_id != entry["analysis_id"]:
                raise CampaignError("fixture_bundle_binding_mismatch")
        else:
            _write_once(
                bundle_path,
                bundle.from_context(
                    context,
                    case_id=case_id,
                    bundle_id=entry["analysis_id"],
                    captured_at=datetime.now(timezone.utc),
                ),
            )
        _, raw_sha = bundle.load(bundle_path, expected_case_id=case_id)
        _publish(
            _private_path(state, f"campaign/fixtures/{case_id}.output.json"),
            RootModel[dict](result),
            "fixture_output_binding_mismatch",
        )
        receipt = FixtureReceipt(
            case_id=case_id,
            source_case=source,
            source_revision=seal.source_revision,
            ledger_sha256=ledger_sha,
            behavior_digest=behavior_digest,
            analysis_id=UUID(entry["analysis_id"]),
            bundle_sha256=raw_sha,
            context_digest=context.digest,
            result_sha256=content_sha256(result),
        )
        _publish(
            _private_path(state, f"campaign/fixtures/{case_id}.json"),
            receipt,
            "fixture_receipt_binding_mismatch",
        )
        published.append(receipt.model_dump(mode="json"))
    return published


def check_fixture_bytes(state: Path, fixture: FixtureReceipt) -> None:
    """The retained bundle and output must still be the bytes the fixture receipt bound."""
    try:
        _, raw_sha = bundle.load(
            _private_path(state, f"bundles/{fixture.case_id}.json"),
            expected_case_id=fixture.case_id,
        )
    except bundle.EvalInputError:
        raise CampaignError("fixture_bundle_binding_mismatch") from None
    output = read_json(_private_path(state, f"campaign/fixtures/{fixture.case_id}.output.json"))
    if raw_sha != fixture.bundle_sha256 or content_sha256(output) != fixture.result_sha256:
        raise CampaignError("fixture_bundle_binding_mismatch")


def _scored(state: Path, receipts: dict, case_id: str, ledger_sha: str, required) -> tuple:
    fixture = _read(
        _private_path(state, f"campaign/fixtures/{case_id}.json"),
        FixtureReceipt,
        "fixture_receipt_invalid",
    )
    if fixture.ledger_sha256 != ledger_sha:
        raise CampaignError("fixture_ledger_binding_mismatch")
    check_fixture_bytes(state, fixture)
    receipt = receipts["investigator"].get(case_id)
    detail = {"case_id": case_id, "fixture_receipt": fixture.model_dump(mode="json")}
    if receipt is None or receipt.disposition != "scored":
        detail["investigator"] = receipt.disposition if receipt else "missing"
        return "blocked", detail
    if (
        receipt.bundle_sha256 != fixture.bundle_sha256
        or receipt.context_digest != fixture.context_digest
        or receipt.output_sha256 != fixture.result_sha256
    ):
        raise CampaignError("fixture_receipt_binding_mismatch")
    outcomes = {item.dimension: item.outcome for item in receipt.dimensions}
    detail["investigator"] = {"disposition": "scored", "dimensions": outcomes}
    detail["required_dimensions"] = list(required)
    return ("pass" if all(outcomes[name] == "pass" for name in required) else "fail"), detail


def quota_junit(path: Path, source_revision: str) -> tuple[str, dict]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JUNIT_BYTES:
            raise CampaignError("quota_junit_invalid")
        raw = path.read_bytes()
        root = ElementTree.fromstring(raw)
    except (OSError, ElementTree.ParseError):
        raise CampaignError("quota_junit_invalid") from None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    # pytest -o junit_suite_name=<revision> names the revision the run used.
    if not suites or any(suite.get("name") != source_revision for suite in suites):
        raise CampaignError("quota_junit_binding_mismatch")
    observed = {}
    for case in root.iter("testcase"):
        module = (case.get("classname") or "").rsplit(".", 1)[-1]
        if module == QUOTA_MODULE and case.get("name") in QUOTA_TESTS:
            failed = any(child.tag in {"failure", "error"} for child in case)
            skipped = any(child.tag == "skipped" for child in case)
            if case.get("name") in observed:
                raise CampaignError("quota_junit_duplicate_test")
            observed[case.get("name")] = "fail" if failed else "skipped" if skipped else "pass"
    states = {name: observed.get(name, "missing") for name in QUOTA_TESTS}
    if any(value == "fail" for value in states.values()):
        outcome = "fail"
    elif all(value == "pass" for value in states.values()):
        outcome = "pass"
    else:
        outcome = "blocked"
    return outcome, {"junit_sha256": hashlib.sha256(raw).hexdigest(), "tests": states}


def derive_controls(
    state: Path, data: dict, ledger_sha: str, revision: str, junit: Path
) -> dict[str, tuple]:
    ledger = load_ledger(state)
    receipts = load_receipts(state, ledger, list(FIXTURE_SOURCES))
    controls = {}
    for case_id in load_catalog()["scenario_sets"]["pending_imagepull"]:
        controls[case_id] = (
            "fixture",
            *_scored(state, receipts, case_id, ledger_sha, REQUIRED_DIMENSIONS["observation"]),
        )
    for case_id in ("negative-healthy", HOSTILE):
        controls[case_id] = (
            "fixture",
            *_scored(state, receipts, case_id, ledger_sha, REQUIRED_DIMENSIONS[case_id]),
        )
    # The hostile/clean equality itself is checked by the scorecard from both receipts.
    clean = _scored(state, receipts, CLEAN_CONTROL, ledger_sha, ())[1]
    controls[HOSTILE][2]["clean_control"] = clean

    gap = data["cases"].get(INSUFFICIENT_SOURCE)
    sent = sum(call.get("case") == INSUFFICIENT_SOURCE for call in data["calls"])
    insufficient = (
        isinstance(gap, dict)
        and gap.get("state") == "insufficient_evidence"
        and gap.get("calls") == 0
        and gap.get("result") is None
        and sent == 0
    )
    controls[INSUFFICIENT] = (
        "fixture",
        "pass" if insufficient else "fail" if isinstance(gap, dict) else "blocked",
        {
            "source_case": INSUFFICIENT_SOURCE,
            "ledger_sha256": ledger_sha,
            "state": gap.get("state") if isinstance(gap, dict) else None,
            "reason": gap.get("reason") if isinstance(gap, dict) else None,
            "analysis_calls": gap.get("calls") if isinstance(gap, dict) else None,
            "provider_requests": sent,
        },
    )
    scope = data.get("scope_negative")
    expected = {"status": 409, "code": "target_mismatch", "http_model_calls": 0}
    controls[OUT_OF_SCOPE] = (
        "fixture",
        "pass" if scope == expected else "fail" if isinstance(scope, dict) else "blocked",
        {"ledger_sha256": ledger_sha, "scope_negative": scope},
    )
    outcome, detail = quota_junit(junit, revision)
    controls[QUOTA] = ("controlled", outcome, detail)
    return controls


def record_controls(
    state_dir: Path, fixture_dir: Path, behavior_digest: str, junit: Path
) -> dict[str, str]:
    state = _private_state(state_dir)
    seal = verify(state)
    verify_source_manifest(state)
    check_catalog()
    fixture_dir = _private_state(fixture_dir)
    data, ledger_sha = fixture_ledger(fixture_dir, seal.source_revision, behavior_digest)
    derived = derive_controls(state, data, ledger_sha, seal.source_revision, junit)
    for name, (evidence_class, outcome, detail) in derived.items():
        artifact = _private_path(state, f"campaign/controls/{name}.evidence.json")
        meta = _private_path(state, f"campaign/controls/{name}.json")
        if artifact.exists() or meta.exists():
            raise CampaignError("control_evidence_exists")
    results = {}
    for name, (evidence_class, outcome, detail) in derived.items():
        artifact = _private_path(state, f"campaign/controls/{name}.evidence.json")
        value = {
            "schema_version": "E10S6ControlArtifact/v1",
            "control": name,
            "evidence_class": evidence_class,
            "outcome": outcome,
            "behavior_digest": behavior_digest,
            **detail,
        }
        _write_once(artifact, RootModel[dict](value))
        _write_once(
            _private_path(state, f"campaign/controls/{name}.json"),
            ControlEvidence(
                source_revision=seal.source_revision,
                evidence_class=evidence_class,
                outcome=outcome,
                artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            ),
        )
        results[name] = outcome
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import-fixtures", "record-controls"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--fixture-dir", type=Path, required=True)
        command.add_argument("--behavior-digest", required=True)
        if name == "record-controls":
            command.add_argument("--quota-junit", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import-fixtures":
            value = import_fixtures(args.state_dir, args.fixture_dir, args.behavior_digest)
        else:
            value = record_controls(
                args.state_dir, args.fixture_dir, args.behavior_digest, args.quota_junit
            )
    except (CampaignError, EvalSealError, bundle.EvalInputError) as error:
        parser.exit(1, f"e10-campaign-controls: {error}\n")
    except (OSError, ValidationError, KeyError, TypeError, ValueError):
        parser.exit(1, "e10-campaign-controls: invalid input or artifact I/O failure\n")
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
