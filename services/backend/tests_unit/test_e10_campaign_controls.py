"""S6 observation/negative controls: controlled-fixture product results into the scorecard."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cloudops.features.evaluation import cli
from cloudops.features.evaluation.rules import compare
from eval_fixtures import context_for_case, result_for

ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
BEHAVIOR = "f" * 64


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import e10_campaign_controls

    return e10_campaign_controls


@pytest.fixture
def state(controls, tmp_path):
    from e10_campaign_plan import main

    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    arguments = ["--state-dir", str(directory), "--cohort-id", "controls"]
    assert main([*arguments, "--source-revision", REVISION]) == 0
    return directory


def fixture_run(controls, root: Path, **changes) -> Path:
    directory = root / "fixture"
    (directory / "contexts").mkdir(mode=0o700, parents=True)
    directory.chmod(0o700)
    identity = {"behavior_digest": BEHAVIOR, "model": "fixture-luna"}
    cases = {}
    for source in controls.FIXTURE_SOURCES.values():
        analysis_id = str(uuid4())
        context = context_for_case(source).model_copy(update={"analysis_identity": analysis_id})
        (directory / "contexts" / f"{source}.json").write_text(context.model_dump_json())
        cases[source] = {
            "analysis_id": analysis_id,
            "run_id": str(context.run_id),
            "purpose": "evaluation",
            "state": "completed",
            "reason": "validated_advice",
            "identity": identity,
            "calls": 1,
            "repairs": 0,
            "result": result_for(context, compare(context)).model_dump(mode="json"),
        }
    cases["n-required-gap"] = {
        "analysis_id": str(uuid4()),
        "run_id": str(uuid4()),
        "purpose": "evaluation",
        "state": "insufficient_evidence",
        "reason": "no_evidence",
        "identity": identity,
        "calls": 0,
        "repairs": 0,
        "result": None,
    }
    ledger = {
        "source_commit": REVISION,
        "evaluation_finished": True,
        "cases_sha256": hashlib.sha256((ROOT / controls.E06_CASES).read_bytes()).hexdigest(),
        "primary_identity": identity,
        "calls": [{"case": source} for source in controls.FIXTURE_SOURCES.values()],
        "cases": cases,
        "scope_negative": {"status": 409, "code": "target_mismatch", "http_model_calls": 0},
    }
    for key, value in changes.items():
        if value is None:
            ledger.pop(key)
        else:
            ledger[key] = value
    (directory / "ledger.json").write_text(json.dumps(ledger))
    return directory


def junit(root: Path, *, suite=REVISION, states=None) -> Path:
    states = states or {}
    cases = []
    for name in (
        "test_unavailable_zero_network[unqualified]",
        "test_unavailable_zero_network[disabled]",
        "test_unavailable_zero_network[unconfigured]",
        "test_quota_hold_resume_keeps_budgets_and_deadline",
    ):
        state = states.get(name, "pass")
        if state == "missing":
            continue
        child = {"pass": "", "fail": "<failure message='x'/>", "skipped": "<skipped/>"}[state]
        cases.append(
            f'<testcase classname="tests.test_diagnosis_lifecycle" name="{name}">{child}</testcase>'
        )
    path = root / "quota.xml"
    path.write_text(
        f'<testsuites><testsuite name="{suite}">{"".join(cases)}</testsuite></testsuites>'
    )
    return path


def score(directory: Path, case_id: str) -> None:
    common = ["--state-dir", str(directory), "--case-id", case_id]
    common += ["--bundle", f"bundles/{case_id}.json"]
    assert cli.main(["compare", *common, "--output", f"outputs/rules/{case_id}.json"]) == 0
    for method, extra in (
        ("rules", []),
        ("investigator", ["--method-output", f"campaign/fixtures/{case_id}.output.json"]),
    ):
        arguments = ["--method", method, "--disposition", "scored"]
        arguments += ["--output", f"receipts/{method}/{case_id}.json", *extra]
        assert cli.main(["score", *common, *arguments]) == 0


def test_fixed_mapping_matches_the_release_catalog(controls):
    controls.check_catalog()


def test_fixture_controls_reach_the_scorecard(controls, state, tmp_path):
    from e10_campaign_report import summarize

    fixtures = fixture_run(controls, tmp_path)
    published = controls.import_fixtures(state, fixtures, BEHAVIOR)
    assert [item["case_id"] for item in published] == list(controls.FIXTURE_SOURCES)
    # An interrupted import can be repeated without changing any published byte.
    assert controls.import_fixtures(state, fixtures, BEHAVIOR) == published
    for case_id in controls.FIXTURE_SOURCES:
        score(state, case_id)
    results = controls.record_controls(state, fixtures, BEHAVIOR, junit(tmp_path))
    receipts = {
        case_id: json.loads((state / f"receipts/investigator/{case_id}.json").read_text())
        for case_id in controls.FIXTURE_SOURCES
    }
    for case_id, required in (
        *(
            (case, controls.REQUIRED_DIMENSIONS["observation"])
            for case in list(controls.FIXTURE_SOURCES)[:4]
        ),
        ("negative-healthy", controls.REQUIRED_DIMENSIONS["negative-healthy"]),
        (controls.HOSTILE, controls.REQUIRED_DIMENSIONS[controls.HOSTILE]),
    ):
        outcomes = {item["dimension"]: item["outcome"] for item in receipts[case_id]["dimensions"]}
        expected = "pass" if all(outcomes[name] == "pass" for name in required) else "fail"
        assert results[case_id] == expected
    assert results[controls.INSUFFICIENT] == "pass"
    # The controlled outputs are the rules advice, which satisfies these labels.
    assert {results[case] for case in list(controls.FIXTURE_SOURCES)[:-1]} == {"pass"}
    assert results[controls.OUT_OF_SCOPE] == "pass"
    assert results[controls.QUOTA] == "pass"

    report = summarize(state)
    for name, outcome in results.items():
        recorded = report["controls"][name]
        assert recorded["evidence_class"] == ("controlled" if name == controls.QUOTA else "fixture")
        if name != controls.HOSTILE:
            assert recorded["outcome"] == outcome
    assert report["control_counts"]["observations"] == {"planned": 4, "passed": 4}
    assert report["control_counts"]["negatives"] == {"planned": 5, "passed": 5}
    assert report["hostile_clean_pair"] == {"rules": "pass", "investigator": "pass"}
    artifact = json.loads((state / "campaign/controls/negative-healthy.evidence.json").read_text())
    assert artifact["fixture_receipt"]["behavior_digest"] == BEHAVIOR
    assert artifact["outcome"] == results["negative-healthy"]
    with pytest.raises(controls.CampaignError, match="control_evidence_exists"):
        controls.record_controls(state, fixtures, BEHAVIOR, junit(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_commit": "b" * 40},
        {"evaluation_finished": None},
        {"cases_sha256": "0" * 64},
        {"primary_identity": {"behavior_digest": "e" * 64}},
    ],
)
def test_import_refuses_another_revision_identity_or_unfinished_run(
    controls, state, tmp_path, changes
):
    fixtures = fixture_run(controls, tmp_path, **changes)
    with pytest.raises(controls.CampaignError, match="fixture_ledger_binding_mismatch"):
        controls.import_fixtures(state, fixtures, BEHAVIOR)
    assert not (state / "bundles").exists()


def test_import_refuses_a_context_the_result_did_not_use(controls, state, tmp_path):
    fixtures = fixture_run(controls, tmp_path)
    ledger = json.loads((fixtures / "ledger.json").read_text())
    ledger["cases"]["c07"]["result"]["context_digest"] = "0" * 64
    (fixtures / "ledger.json").write_text(json.dumps(ledger))
    with pytest.raises(controls.CampaignError, match="fixture_context_binding_mismatch"):
        controls.import_fixtures(state, fixtures, BEHAVIOR)
    assert not (state / "bundles").exists()


def test_import_refuses_a_changed_output_after_publication(controls, state, tmp_path):
    fixtures = fixture_run(controls, tmp_path)
    controls.import_fixtures(state, fixtures, BEHAVIOR)
    output = state / "campaign/fixtures/pending-insufficient-cpu.output.json"
    output.write_text(output.read_text().replace('"supported"', '"insufficient_evidence"'))
    with pytest.raises(controls.CampaignError, match="fixture_output_binding_mismatch"):
        controls.import_fixtures(state, fixtures, BEHAVIOR)


def test_unscored_cases_and_failed_zero_call_negatives_are_not_passes(controls, state, tmp_path):
    calls = [{"case": source} for source in [*controls.FIXTURE_SOURCES.values(), "n-required-gap"]]
    scope = {"status": 201, "code": None, "http_model_calls": 1}
    fixtures = fixture_run(controls, tmp_path, calls=calls, scope_negative=scope)
    controls.import_fixtures(state, fixtures, BEHAVIOR)
    states = {"test_unavailable_zero_network[disabled]": "fail"}
    results = controls.record_controls(state, fixtures, BEHAVIOR, junit(tmp_path, states=states))
    # No receipt was scored: every receipt-based control is blocked, never passed.
    assert {results[case] for case in list(controls.FIXTURE_SOURCES)[:-1]} == {"blocked"}
    assert results[controls.INSUFFICIENT] == "fail"
    assert results[controls.OUT_OF_SCOPE] == "fail"
    assert results[controls.QUOTA] == "fail"


def test_controls_refuse_a_ledger_changed_after_import(controls, state, tmp_path):
    fixtures = fixture_run(controls, tmp_path)
    controls.import_fixtures(state, fixtures, BEHAVIOR)
    ledger = json.loads((fixtures / "ledger.json").read_text())
    ledger["scope_negative"]["status"] = 409
    (fixtures / "ledger.json").write_text(json.dumps(ledger, indent=1))
    with pytest.raises(controls.CampaignError, match="fixture_ledger_binding_mismatch"):
        controls.record_controls(state, fixtures, BEHAVIOR, junit(tmp_path))
    assert not (state / "campaign/controls").exists()


@pytest.mark.parametrize(
    "states,expected",
    [
        ({}, "pass"),
        ({"test_quota_hold_resume_keeps_budgets_and_deadline": "missing"}, "blocked"),
        ({"test_unavailable_zero_network[unqualified]": "skipped"}, "blocked"),
        ({"test_unavailable_zero_network[unconfigured]": "fail"}, "fail"),
    ],
)
def test_quota_control_needs_every_named_test_to_pass(controls, tmp_path, states, expected):
    outcome, detail = controls.quota_junit(junit(tmp_path, states=states), REVISION)
    assert outcome == expected
    assert len(detail["tests"]) == 4


def test_quota_report_must_name_the_sealed_revision(controls, tmp_path):
    with pytest.raises(controls.CampaignError, match="quota_junit_binding_mismatch"):
        controls.quota_junit(junit(tmp_path, suite="b" * 40), REVISION)
