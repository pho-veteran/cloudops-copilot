"""S6 per-phase timing derived from retained lab and product receipts."""

import json
import os
from pathlib import Path

import pytest

from cloudops.features.evaluation.schemas import EvalStage

ROOT = Path(__file__).resolve().parents[3]
TRIAL = {
    "phases": {
        "baseline": "2026-11-25T10:00:00+00:00",
        "inject": "2026-11-25T10:01:00+00:00",
        "detect": "2026-11-25T10:01:30.250+00:00",
    }
}
PRODUCT = {
    "incident": {"created_at": "2026-11-25T10:01:40Z"},
    "run": {"created_at": "2026-11-25T10:01:45Z", "finished_at": "2026-11-25T10:02:05Z"},
    "analysis": {
        "attempt_history": [
            {
                "number": 2,
                "started_at": "2026-11-25T10:02:30Z",
                "finished_at": "2026-11-25T10:03:00Z",
            },
            {
                "number": 1,
                "started_at": "2026-11-25T10:02:06Z",
                "finished_at": "2026-11-25T10:02:25Z",
            },
        ]
    },
    "plan": {"created_at": "2026-11-25T10:03:10Z", "decided_at": "2026-11-25T10:04:10Z"},
    "execution": {"created_at": "2026-11-25T10:04:11Z"},
    "verification": {
        "acknowledged_at": "2026-11-25T10:04:41Z",
        "finalized_at": "2026-11-25T10:07:41Z",
    },
}


@pytest.fixture
def timing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import e10_campaign_timing

    return e10_campaign_timing


def derive(timing, *, action_expected=True, **changes):
    inputs = {**PRODUCT, **changes}
    return timing.derive(
        TRIAL,
        incident=inputs["incident"],
        run=inputs["run"],
        analysis=inputs["analysis"],
        plan=inputs["plan"],
        approval=None,
        execution=inputs["execution"],
        verification=inputs["verification"],
        action_expected=action_expected,
    )


def test_recovery_trial_has_all_seven_stages_from_their_own_clocks(timing):
    stages = derive(timing)
    assert [item["stage"] for item in stages] == list(timing.STAGES)
    durations = {item["stage"]: item["duration_ms"] for item in stages}
    assert durations == {
        "inject": 30250,
        "detect": 9750,
        "collect": 20000,
        # The first attempt starts the stage and the last one ends it, in number order.
        "analyze": 54000,
        "approval": 60000,
        "apply": 30000,
        "verify": 180000,
    }
    # Each value is accepted by the S4 scorer's timing schema.
    assert all(EvalStage.model_validate(item).state == "observed" for item in stages)


def test_missing_timestamps_stay_missing_instead_of_zero(timing):
    stages = derive(timing, analysis={"attempt_history": []}, verification={})
    states = {item["stage"]: item["state"] for item in stages}
    assert states["analyze"] == states["apply"] == states["verify"] == "missing"
    assert states["collect"] == "observed"


def test_observation_and_negative_cases_mark_action_stages_not_applicable(timing):
    stages = derive(timing, action_expected=False, plan={}, execution={}, verification={})
    states = {item["stage"]: item["state"] for item in stages}
    assert [states[name] for name in timing.ACTION_STAGES] == ["not_applicable"] * 3
    with pytest.raises(timing.TimingError, match="timing_action_on_non_action_case"):
        derive(timing, action_expected=False)


@pytest.mark.parametrize(
    "changes,code",
    [
        (
            {"run": {"created_at": "2026-11-25T10:02:05Z", "finished_at": "2026-11-25T10:01:45Z"}},
            "timing_negative_interval:collect",
        ),
        ({"incident": {"created_at": "2026-11-25T10:01:40"}}, "timing_timestamp_without_timezone"),
        ({"incident": {"created_at": "not-a-time"}}, "timing_timestamp_invalid"),
    ],
)
def test_reversed_or_ambiguous_timestamps_are_rejected(timing, changes, code):
    with pytest.raises(timing.TimingError, match=code):
        derive(timing, **changes)


def test_cli_writes_a_private_scorer_input_once(timing, tmp_path):
    paths = {}
    for name, value in {"trial": TRIAL, **PRODUCT}.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(value))
    output = tmp_path / "timing.json"
    arguments = [f"--{name}={path}" for name, path in paths.items()]
    assert timing.main([*arguments, f"--output={output}"]) == 0
    written = json.loads(output.read_text())
    assert [item["stage"] for item in written] == list(timing.STAGES)
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        timing.main([*arguments, f"--output={output}"])
