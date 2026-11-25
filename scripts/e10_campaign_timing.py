"""Derive the seven S6 stage durations for one trial from retained product and lab receipts.

Stage boundaries (start -> end), each from one stated clock source:

- inject:   lab fault start (trial phases.inject) -> lab observation of the fault (phases.detect)
- detect:   lab observation -> product incident created_at
- collect:  product Run created_at -> finished_at of the Run's last collection attempt
            (Run.finished_at is cleared at analysis admission and reused for its end)
- analyze:  first Analysis attempt started_at -> last Analysis attempt finished_at
            (AnalysisView.attempt_history; the view has no top-level timestamps)
- approval: product plan created_at -> plan decided_at
- apply:    product execution created_at -> verification acknowledged_at
- verify:   verification acknowledged_at -> verification finalized_at

The lab and product timestamps come from the same host clock (the owned lab and its
database container). A missing timestamp leaves the stage "missing"; a stage that has no
action by design (observation and negative cases) is "not_applicable". A negative or
non-positive interval is rejected rather than reported as zero.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

STAGES = ("inject", "detect", "collect", "analyze", "approval", "apply", "verify")
ACTION_STAGES = ("approval", "apply", "verify")


class TimingError(ValueError):
    pass


def _stamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TimingError("timing_timestamp_invalid") from None
    if parsed.tzinfo is None:
        raise TimingError("timing_timestamp_without_timezone")
    return parsed


def _get(record: dict | None, *keys):
    value = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _stage(name: str, start, end) -> dict:
    begin, finish = _stamp(start), _stamp(end)
    if begin is None or finish is None:
        return {"stage": name, "state": "missing"}
    milliseconds = (finish - begin).total_seconds() * 1000
    if milliseconds < 0:
        raise TimingError(f"timing_negative_interval:{name}")
    return {"stage": name, "state": "observed", "duration_ms": max(1, math.ceil(milliseconds))}


def _attempt_span(history) -> tuple:
    """First attempt's start and last attempt's finish, by attempt number."""
    if not isinstance(history, list):
        return None, None
    attempts = [item for item in history if isinstance(item, dict)]
    numbers = [item.get("number") for item in attempts]
    if not attempts or any(type(number) is not int for number in numbers):
        return None, None
    if len(set(numbers)) != len(numbers):
        raise TimingError("timing_attempt_number_duplicated")
    attempts.sort(key=lambda item: item["number"])
    return attempts[0].get("started_at"), attempts[-1].get("finished_at")


def derive(
    trial: dict,
    *,
    incident: dict | None,
    run: dict | None,
    analysis: dict | None,
    plan: dict | None,
    approval: dict | None,
    execution: dict | None,
    verification: dict | None,
    action_expected: bool,
) -> list[dict]:
    phases = trial.get("phases") or {}
    plan_record = (approval or {}).get("plan") if isinstance(approval, dict) else None
    plan_record = plan_record or approval or plan
    analysis_start, analysis_end = _attempt_span(_get(analysis, "attempt_history"))
    # Run.finished_at is cleared when analysis is admitted and later set to the analysis
    # end, so collection ends with its own last attempt (RunView.attempts).
    _, collection_end = _attempt_span(_get(run, "attempts"))
    values = {
        "inject": _stage("inject", phases.get("inject"), phases.get("detect")),
        "detect": _stage("detect", phases.get("detect"), _get(incident, "created_at")),
        "collect": _stage("collect", _get(run, "created_at"), collection_end),
        "analyze": _stage("analyze", analysis_start, analysis_end),
        "approval": _stage(
            "approval", _get(plan_record, "created_at"), _get(plan_record, "decided_at")
        ),
        "apply": _stage(
            "apply", _get(execution, "created_at"), _get(verification, "acknowledged_at")
        ),
        "verify": _stage(
            "verify", _get(verification, "acknowledged_at"), _get(verification, "finalized_at")
        ),
    }
    if not action_expected:
        for name in ACTION_STAGES:
            if values[name]["state"] == "observed":
                raise TimingError("timing_action_on_non_action_case")
            values[name] = {"stage": name, "state": "not_applicable"}
    return [values[name] for name in STAGES]


def _optional(path: Path | None):
    if path is None:
        return None
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
        raise TimingError("timing_input_unreadable")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--incident", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--execution", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--no-action", action="store_true", help="Observation or negative case.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        stages = derive(
            _optional(args.trial),
            incident=_optional(args.incident),
            run=_optional(args.run),
            analysis=_optional(args.analysis),
            plan=_optional(args.plan),
            approval=_optional(args.approval),
            execution=_optional(args.execution),
            verification=_optional(args.verification),
            action_expected=not args.no_action,
        )
        if args.output.exists():
            raise TimingError("timing_output_exists")
        args.output.write_text(json.dumps(stages, indent=2) + "\n", encoding="utf-8")
        args.output.chmod(0o600)
    except (TimingError, OSError, ValueError) as error:
        parser.exit(1, f"e10-campaign-timing: {error}\n")
    print(json.dumps(stages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
