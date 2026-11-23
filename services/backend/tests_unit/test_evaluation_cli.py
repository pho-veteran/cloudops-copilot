import hashlib
import json
import shutil

from cloudops.features.evaluation import cli
from cloudops.features.evaluation import seal
from eval_fixtures import CASES, CONTROLLED_OUTCOMES, write_corpus

SOURCE_REVISION = "0f6afb146e515d3296ffb2b31348226d3c5f037c"


def _invoke(*arguments):
    assert cli.main(list(arguments)) == 0


def test_compare_does_not_read_held_out_labels_or_oracle(tmp_path, monkeypatch):
    fixture_root = tmp_path / "fixtures"
    state = tmp_path / "state"
    write_corpus(fixture_root)
    _invoke(
        "freeze",
        "--state-dir",
        str(state),
        "--cohort",
        str(fixture_root / "cohort.json"),
        "--labels",
        str(fixture_root / "labels.json"),
        "--rubric",
        str(fixture_root / "rubric.json"),
        "--source-revision",
        SOURCE_REVISION,
    )

    case = next(spec for spec in CASES if "rules" in spec.methods)
    label_paths = {
        (state / "inputs/labels.json").resolve(),
        (state / "inputs/rubric.json").resolve(),
        (state / "source/services/backend/tests/eval_fixtures.py").resolve(),
        (state / "source/services/backend/tests/fixtures/e06/cases.json").resolve(),
        (state / "source/services/backend/tests/fixtures/e06/oracle.json").resolve(),
    }
    original_read_bytes = type(state).read_bytes

    def guarded_read_bytes(path):
        if path.resolve() in label_paths:
            raise AssertionError(f"comparison read held-out input: {path}")
        return original_read_bytes(path)

    def unexpected_load_inputs(*_args, **_kwargs):
        raise AssertionError("comparison deserialized scoring inputs")

    monkeypatch.setattr(type(state), "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(cli, "load_inputs", unexpected_load_inputs)
    monkeypatch.setattr(seal, "load_inputs", unexpected_load_inputs)
    _invoke(
        "compare",
        "--state-dir",
        str(state),
        "--case-id",
        case.case_id,
        "--bundle",
        str(state / "bundles" / f"{case.case_id}.json"),
        "--output",
        str(state / "outputs/rules" / f"{case.case_id}.json"),
    )


def test_controlled_cli_keeps_paired_evidence_and_every_disposition(tmp_path, capsys):
    fixture_root = tmp_path / "fixtures"
    state = tmp_path / "state"
    write_corpus(fixture_root)

    _invoke(
        "freeze",
        "--state-dir",
        str(state),
        "--cohort",
        str(fixture_root / "cohort.json"),
        "--labels",
        str(fixture_root / "labels.json"),
        "--rubric",
        str(fixture_root / "rubric.json"),
        "--source-revision",
        SOURCE_REVISION,
    )
    shutil.copytree(
        fixture_root / "method-outputs/investigator",
        state / "method-outputs/investigator",
    )
    timing_path = state / "timing/controlled.json"
    timing_path.parent.mkdir(parents=True)
    timing_path.write_text(
        json.dumps(
            [
                {"stage": name, "state": "missing"}
                for name in ("inject", "detect", "collect", "analyze")
            ]
            + [
                {"stage": name, "state": "not_applicable"}
                for name in ("approval", "apply", "verify")
            ]
        ),
        encoding="utf-8",
    )

    for spec in CASES:
        if "rules" not in spec.methods:
            continue
        _invoke(
            "compare",
            "--state-dir",
            str(state),
            "--case-id",
            spec.case_id,
            "--bundle",
            str(state / "bundles" / f"{spec.case_id}.json"),
            "--output",
            str(state / "outputs/rules" / f"{spec.case_id}.json"),
        )

    for spec in CASES:
        if "rules" not in spec.methods:
            continue
        disposition = "blocked" if spec.case_id == "malformed-evidence" else "scored"
        args = [
            "score",
            "--state-dir",
            str(state),
            "--case-id",
            spec.case_id,
            "--method",
            "rules",
            "--bundle",
            str(state / "bundles" / f"{spec.case_id}.json"),
            "--timing",
            str(timing_path),
            "--disposition",
            disposition,
            "--output",
            str(state / "receipts/rules" / f"{spec.case_id}.json"),
        ]
        if disposition == "blocked":
            args.extend(["--reason", "required_source_unavailable"])
        else:
            args.extend(
                [
                    "--method-output",
                    str(state / "outputs/rules" / f"{spec.case_id}.json"),
                ]
            )
        _invoke(*args)

    for spec in CASES:
        if "investigator" not in spec.methods:
            continue
        disposition, reason = CONTROLLED_OUTCOMES.get(spec.case_id, ("scored", None))
        args = [
            "score",
            "--state-dir",
            str(state),
            "--case-id",
            spec.case_id,
            "--method",
            "investigator",
            "--bundle",
            str(state / "bundles" / f"{spec.case_id}.json"),
            "--timing",
            str(timing_path),
            "--disposition",
            disposition,
            "--output",
            str(state / "receipts/investigator" / f"{spec.case_id}.json"),
        ]
        if reason is not None:
            args.extend(["--reason", reason])
        else:
            args.extend(
                [
                    "--method-output",
                    str(state / "method-outputs/investigator" / f"{spec.case_id}.json"),
                ]
            )
        _invoke(*args)

    for method in ("rules", "investigator"):
        _invoke(
            "summary",
            "--state-dir",
            str(state),
            "--method",
            method,
            "--output",
            str(state / "summaries" / f"{method}.json"),
        )
    _invoke(
        "compare-summary",
        "--state-dir",
        str(state),
        "--output",
        str(state / "comparison.json"),
    )
    _invoke("verify", "--state-dir", str(state))

    rules = json.loads((state / "summaries/rules.json").read_text(encoding="utf-8"))
    investigator = json.loads((state / "summaries/investigator.json").read_text(encoding="utf-8"))
    assert rules["dispositions"] == {
        "scored": 9,
        "failed": 0,
        "missing": 0,
        "blocked": 1,
        "interrupted": 0,
    }
    assert investigator["dispositions"] == {
        "scored": 9,
        "failed": 1,
        "missing": 0,
        "blocked": 0,
        "interrupted": 1,
    }
    # Non-scored trials stay in their scenario group and lower its rate.
    assert rules["threshold_reasons"] == [
        "unsafe_candidate_max:1>0",
        "rules_abstention_correct_min:0.5<1.0",
    ]
    assert investigator["threshold_reasons"] == [
        "required_candidate_miss_max:1>0",
        "rules_positive_correct_min:0.75<1.0",
    ]
    for stage_name in ("approval", "apply", "verify"):
        rules_stage = next(stage for stage in rules["stages"] if stage["stage"] == stage_name)
        investigator_stage = next(
            stage for stage in investigator["stages"] if stage["stage"] == stage_name
        )
        assert (rules_stage["not_applicable"], investigator_stage["not_applicable"]) == (10, 11)
    assert next(stage for stage in rules["stages"] if stage["stage"] == "analyze")["observed"] == 10
    assert (
        next(stage for stage in investigator["stages"] if stage["stage"] == "analyze")["missing"]
        == 11
    )
    comparison = json.loads((state / "comparison.json").read_text(encoding="utf-8"))
    assert {item["case_id"] for item in comparison["differences"]} == {
        "plan-missing",
        "plan-unsafe",
    }

    (state / "comparison.json").unlink()
    changed_summary = {**rules, "thresholds_met": True}
    (state / "summaries/rules.json").write_text(json.dumps(changed_summary), encoding="utf-8")
    assert (
        cli.main(
            [
                "compare-summary",
                "--state-dir",
                str(state),
                "--output",
                str(state / "comparison.json"),
            ]
        )
        == 2
    )
    assert "summary_mismatch" in capsys.readouterr().out
    assert not (state / "comparison.json").exists()
    (state / "summaries/rules.json").write_text(
        json.dumps(rules, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _invoke(
        "compare-summary",
        "--state-dir",
        str(state),
        "--output",
        str(state / "comparison.json"),
    )
    _invoke("verify", "--state-dir", str(state))


def _freeze_corpus(tmp_path, *, corrupt=()):
    fixture_root = tmp_path / "fixtures"
    state = tmp_path / "state"
    write_corpus(fixture_root)
    for case_id in corrupt:
        path = fixture_root / "bundles" / f"{case_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["manifest_sha256"] = "0" * 64
        path.write_bytes(json.dumps(value).encode("utf-8"))
    _invoke(
        "freeze",
        "--state-dir",
        str(state),
        "--cohort",
        str(fixture_root / "cohort.json"),
        "--labels",
        str(fixture_root / "labels.json"),
        "--rubric",
        str(fixture_root / "rubric.json"),
        "--source-revision",
        SOURCE_REVISION,
    )
    return state


def _score(state, case_id, method, disposition, reason=None):
    args = [
        "score",
        "--state-dir",
        str(state),
        "--case-id",
        case_id,
        "--method",
        method,
        "--bundle",
        str(state / "bundles" / f"{case_id}.json"),
        "--disposition",
        disposition,
        "--output",
        str(state / "receipts" / method / f"{case_id}.json"),
    ]
    if reason is not None:
        args.extend(["--reason", reason])
    return cli.main(args)


def test_invalid_captured_bundle_is_recorded_as_blocked(tmp_path, capsys):
    state = _freeze_corpus(tmp_path, corrupt=("crashloop-positive",))
    compare_args = [
        "compare",
        "--state-dir",
        str(state),
        "--case-id",
        "crashloop-positive",
        "--bundle",
        str(state / "bundles/crashloop-positive.json"),
        "--output",
        str(state / "outputs/rules/crashloop-positive.json"),
    ]
    assert cli.main(compare_args) == 2
    assert "manifest_mismatch" in capsys.readouterr().out
    assert _score(state, "crashloop-positive", "rules", "failed", "method_timeout") == 2
    assert "manifest_mismatch" in capsys.readouterr().out
    # A valid bundle cannot claim to be invalid.
    assert _score(state, "oom-positive", "rules", "blocked", "bundle_invalid") == 2
    assert "disposition_not_allowed" in capsys.readouterr().out

    raw = (state / "bundles/crashloop-positive.json").read_bytes()
    for method in ("rules", "investigator"):
        assert _score(state, "crashloop-positive", method, "blocked", "bundle_invalid") == 0
        receipt = json.loads(
            (state / "receipts" / method / "crashloop-positive.json").read_text(encoding="utf-8")
        )
        assert receipt["disposition"] == "blocked"
        assert receipt["reasons"] == ["bundle_invalid"]
        assert receipt["bundle_sha256"] == hashlib.sha256(raw).hexdigest()
        assert receipt["context_digest"] is None
        assert {item["outcome"] for item in receipt["dimensions"]} == {"blocked"}


def test_rules_trial_without_compare_can_be_recorded_and_summarized(tmp_path):
    state = _freeze_corpus(tmp_path)
    for spec in CASES:
        if "rules" not in spec.methods:
            continue
        outcome = (
            ("blocked", "required_source_unavailable")
            if spec.case_id == "malformed-evidence"
            else ("failed", "method_timeout")
        )
        assert _score(state, spec.case_id, "rules", *outcome) == 0
    _invoke(
        "summary",
        "--state-dir",
        str(state),
        "--method",
        "rules",
        "--output",
        str(state / "summaries/rules.json"),
    )
    summary = json.loads((state / "summaries/rules.json").read_text(encoding="utf-8"))
    assert (summary["planned"], summary["dispositions"]["failed"]) == (10, 9)
    assert summary["thresholds"]["rules_positive_correct_min"] == 0.0
