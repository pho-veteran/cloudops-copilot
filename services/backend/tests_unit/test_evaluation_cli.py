import json
import shutil

from cloudops.features.evaluation import cli
from cloudops.features.evaluation import seal
from eval_fixtures import CASES, write_corpus

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
        disposition = {
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(spec.treatment, "scored")
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
        if disposition == "failed":
            args.extend(["--reason", "no_method_output"])
        elif disposition == "interrupted":
            args.extend(["--reason", "operator_stopped"])
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
    assert rules["threshold_reasons"] == ["unsafe_candidate_max:1>0"]
    assert investigator["threshold_reasons"] == ["required_candidate_miss_max:1>0"]
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
