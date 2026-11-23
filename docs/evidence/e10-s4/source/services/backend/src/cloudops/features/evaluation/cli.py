"""CLI for freezing, comparing, scoring and reporting controlled evaluations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from pydantic import ValidationError

from cloudops.features.evaluation.bundle import EvalInputError, load as load_bundle
from cloudops.features.evaluation.hashing import canonical
from cloudops.features.evaluation.rules import compare
from cloudops.features.evaluation.schemas import (
    EvalAction,
    EvalCaseLedger,
    EvalIncident,
    EvalLedger,
    EvalReceipt,
    EvalStage,
    EvalSummary,
)
from cloudops.features.evaluation.scoring import score_case
from cloudops.features.evaluation.seal import (
    EvalSealError,
    freeze,
    load_cohort,
    load_inputs,
    load_ledger,
    verify,
    write_ledger,
)
from cloudops.features.evaluation.summary import summarize
from cloudops.features.evaluation.versions import DIMENSIONS, DISPOSITIONS, STAGES, VERSIONS

REASONS = (
    "required_source_unavailable",
    "no_method_output",
    "method_timeout",
    "provider_unavailable",
    "external_failure",
    "operator_stopped",
    "operator_interrupted",
    "output_too_large",
    "input_invalid",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval-baseline")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("freeze")
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--cohort", type=Path, required=True)
    command.add_argument("--labels", type=Path, required=True)
    command.add_argument("--rubric", type=Path, required=True)
    command.add_argument("--source-revision", required=True)

    command = commands.add_parser("verify")
    command.add_argument("--state-dir", type=Path, required=True)

    command = commands.add_parser("compare")
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--case-id", required=True)
    command.add_argument("--bundle", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)

    command = commands.add_parser("score")
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--case-id", required=True)
    command.add_argument("--method", choices=("rules", "investigator"), required=True)
    command.add_argument("--bundle", type=Path)
    command.add_argument("--method-output", type=Path)
    command.add_argument("--timing", type=Path)
    command.add_argument("--action", type=Path)
    command.add_argument("--incident", type=Path)
    command.add_argument("--disposition", choices=DISPOSITIONS, required=True)
    command.add_argument("--reason", choices=REASONS)
    command.add_argument("--output", type=Path, required=True)

    command = commands.add_parser("summary")
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--method", choices=("rules", "investigator"), required=True)
    command.add_argument("--output", type=Path, required=True)

    command = commands.add_parser("compare-summary")
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    return parser


def _json_bytes(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise EvalSealError("output_exists")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temp, path)
    except FileExistsError:
        raise EvalSealError("output_exists") from None
    finally:
        temp.unlink(missing_ok=True)


def _safe_path(state_dir: Path, path: Path, *, must_exist: bool) -> Path:
    state_root = state_dir.resolve()
    candidate = path if path.is_absolute() else state_root / path
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError:
        raise EvalSealError("file_missing") from None
    if resolved != state_root and state_root not in resolved.parents:
        raise EvalSealError("path_invalid")
    if candidate.is_symlink():
        raise EvalSealError("path_invalid")
    return resolved


def _bundle_path(state_dir: Path, case, argument: Path | None, *, optional: bool = False):
    expected = _safe_path(state_dir, Path(case.bundle), must_exist=not optional)
    if argument is None:
        if optional:
            return expected if expected.exists() else None
        raise EvalSealError("bundle_required")
    provided = _safe_path(state_dir, argument, must_exist=True)
    if provided != expected:
        raise EvalSealError("bundle_path_mismatch")
    return provided


def _output_path(state_dir: Path, path: Path) -> Path:
    return _safe_path(state_dir, path, must_exist=False)


def _find_case(cohort, case_id: str):
    case = next((item for item in cohort.cases if item.case_id == case_id), None)
    if case is None:
        raise EvalSealError("case_not_planned")
    return case


def _replace_case(ledger: EvalLedger, changed: EvalCaseLedger) -> EvalLedger:
    updated = [changed if item.case_id == changed.case_id else item for item in ledger.cases]
    return EvalLedger.model_validate(
        {
            **ledger.model_dump(mode="json"),
            "started": True,
            "cases": [item.model_dump(mode="json") for item in updated],
        }
    )


def _read_json(path: Path, code: str, *, max_bytes: int = 64 * 1024):
    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            raise EvalSealError(code)
        return json.loads(raw.decode("utf-8")), raw
    except EvalSealError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise EvalSealError(code) from None


def _stage_values(path: Path | None, *, analyze_duration: int | None = None) -> list[EvalStage]:
    states: dict[str, EvalStage] = {}
    if path is not None:
        value, _ = _read_json(path, "timing_invalid")
        if not isinstance(value, list):
            raise EvalSealError("timing_invalid")
        try:
            parsed = [EvalStage.model_validate(item) for item in value]
        except ValidationError:
            raise EvalSealError("timing_invalid") from None
        states = {item.stage: item for item in parsed}
        if len(states) != len(parsed):
            raise EvalSealError("timing_duplicate_stage")
    if analyze_duration is not None:
        states["analyze"] = EvalStage(
            stage="analyze", state="observed", duration_ms=analyze_duration
        )
    return [states.get(name, EvalStage(stage=name, state="missing")) for name in STAGES]


def _optional_model(path: Path | None, model_type, default):
    if path is None:
        return default
    value, _ = _read_json(path, "outcome_invalid")
    try:
        return model_type.model_validate(value)
    except ValidationError:
        raise EvalSealError("outcome_invalid") from None


def _load_output(state_dir: Path, path: Path | None, *, required: bool):
    if path is None:
        if required:
            raise EvalSealError("method_output_required")
        return None, None
    resolved = _safe_path(state_dir, path, must_exist=True)
    value, raw = _read_json(resolved, "method_output_invalid", max_bytes=32 * 1024)
    if not isinstance(value, dict):
        raise EvalSealError("method_output_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _do_freeze(args) -> int:
    freeze(args.state_dir, args.cohort, args.labels, args.rubric, args.source_revision)
    print(f"frozen:{hashlib.sha256((args.state_dir / 'seal.json').read_bytes()).hexdigest()}")
    return 0


def _do_verify(args) -> int:
    verify(args.state_dir)
    print("verify:ok")
    return 0


def _do_compare(args) -> int:
    seal = verify(args.state_dir, include_scoring_inputs=False)
    cohort = load_cohort(args.state_dir)
    ledger = load_ledger(args.state_dir)
    case = _find_case(cohort, args.case_id)
    if "rules" not in case.methods:
        raise EvalSealError("method_not_planned")
    entry = next(item for item in ledger.cases if item.case_id == case.case_id)
    if entry.compare_output_sha256 is not None:
        raise EvalSealError("compare_exists")
    bundle_path = _bundle_path(args.state_dir, case, args.bundle)
    expected_output = (args.state_dir / "outputs" / "rules" / f"{case.case_id}.json").resolve()
    if _output_path(args.state_dir, args.output) != expected_output:
        raise EvalSealError("compare_path_mismatch")
    bundle, raw_sha = load_bundle(bundle_path, expected_case_id=case.case_id)
    started = time.perf_counter_ns()
    advice = compare(bundle.context)
    elapsed_ns = time.perf_counter_ns() - started
    duration_ms = max(1, (elapsed_ns + 999_999) // 1_000_000)
    output_raw = canonical(advice).encode("utf-8")
    output_sha = hashlib.sha256(output_raw).hexdigest()
    output_path = _output_path(args.state_dir, args.output)
    _write_new(output_path, output_raw)
    changed = entry.model_copy(
        update={
            "compare_bundle_sha256": raw_sha,
            "compare_context_digest": bundle.context.digest,
            "compare_output_sha256": output_sha,
            "compare_duration_ms": duration_ms,
        }
    )
    write_ledger(args.state_dir, _replace_case(ledger, changed))
    print(f"compare:ok:{case.case_id}:{seal.source_revision}")
    return 0


def _load_receipts(state_dir: Path, ledger: EvalLedger, cohort, method: str):
    entries = {item.case_id: item for item in ledger.cases}
    values = []
    for case in cohort.cases:
        if method not in case.methods:
            continue
        entry = entries[case.case_id]
        expected_sha = entry.receipt_sha256[method]
        if expected_sha is None:
            raise EvalSealError("incomplete_ledger")
        relative = Path("receipts") / method / f"{case.case_id}.json"
        path = _safe_path(state_dir, relative, must_exist=True)
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise EvalSealError("ledger_mismatch")
            values.append(EvalReceipt.model_validate_json(raw))
        except EvalSealError:
            raise
        except (OSError, ValidationError, ValueError):
            raise EvalSealError("receipt_invalid") from None
    return values


def _do_score(args) -> int:
    seal = verify(args.state_dir)
    cohort, labels, rubric = load_inputs(args.state_dir)
    ledger = load_ledger(args.state_dir)
    case = _find_case(cohort, args.case_id)
    if args.method not in case.methods:
        raise EvalSealError("method_not_planned")
    entry = next(item for item in ledger.cases if item.case_id == case.case_id)
    if entry.receipt_sha256[args.method] is not None:
        raise EvalSealError("receipt_exists")
    label = labels.cases[case.case_id]

    bundle_path = _bundle_path(
        args.state_dir, case, args.bundle, optional=args.disposition == "missing"
    )
    bundle = None
    bundle_sha = None
    if bundle_path is not None:
        bundle, bundle_sha = load_bundle(bundle_path, expected_case_id=case.case_id)
    elif args.disposition != "missing":
        raise EvalSealError("bundle_required")

    if "rules" in case.methods and bundle is not None:
        if entry.compare_output_sha256 is None:
            raise EvalSealError("compare_missing")
        if (
            entry.compare_bundle_sha256 != bundle_sha
            or entry.compare_context_digest != bundle.context.digest
        ):
            raise EvalSealError("bundle_binding_mismatch")

    if args.disposition != "scored" and args.method_output is not None:
        raise EvalSealError("output_not_allowed")
    if args.method == "rules":
        if args.disposition == "scored":
            if entry.compare_output_sha256 is None:
                raise EvalSealError("compare_missing")
            method_output_path = args.method_output or (
                Path("outputs/rules") / f"{case.case_id}.json"
            )
            output, output_raw_sha = _load_output(args.state_dir, method_output_path, required=True)
            if output_raw_sha != entry.compare_output_sha256:
                raise EvalSealError("compare_output_mismatch")
        else:
            output = None
    else:
        output, _ = _load_output(
            args.state_dir,
            args.method_output,
            required=args.disposition == "scored",
        )

    if args.disposition in {"blocked", "missing"} and output is not None:
        raise EvalSealError("output_not_allowed")
    if args.disposition in {"failed", "interrupted"} and not args.reason:
        raise EvalSealError("reason_required")
    if args.disposition == "scored" and not output:
        raise EvalSealError("method_output_required")

    missing_required = False
    if bundle is not None:
        coverage = {item.slot: item for item in bundle.context.source_coverage}
        missing_required = any(
            slot not in coverage
            or coverage[slot].status != "available"
            or coverage[slot].content_status != "available"
            for slot in label.required_sources
        )
    if missing_required and args.disposition not in {"blocked", "missing"}:
        raise EvalSealError("disposition_not_allowed")
    if args.disposition == "blocked":
        if not missing_required or args.reason != "required_source_unavailable":
            raise EvalSealError("disposition_not_allowed")
    elif args.reason == "required_source_unavailable":
        raise EvalSealError("disposition_not_allowed")

    action = _optional_model(
        _safe_path(args.state_dir, args.action, must_exist=True) if args.action else None,
        EvalAction,
        EvalAction(state="not_applicable", outcome=None),
    )
    incident = _optional_model(
        _safe_path(args.state_dir, args.incident, must_exist=True) if args.incident else None,
        EvalIncident,
        EvalIncident(disposition="not_applicable"),
    )
    analyze_duration = (
        entry.compare_duration_ms
        if args.method == "rules" and entry.compare_duration_ms is not None
        else None
    )
    timing_path = _safe_path(args.state_dir, args.timing, must_exist=True) if args.timing else None
    timing = _stage_values(timing_path, analyze_duration=analyze_duration)
    reasons = [args.reason] if args.reason else []
    receipt = score_case(
        bundle=bundle,
        bundle_sha256=bundle_sha,
        label=label,
        rubric=rubric,
        method=args.method,
        output=output,
        disposition=args.disposition,
        action=action,
        incident=incident,
        timing=timing,
        reasons=reasons,
    )
    receipt_path = _output_path(args.state_dir, args.output)
    expected_receipt = (
        args.state_dir / "receipts" / args.method / f"{case.case_id}.json"
    ).resolve()
    if receipt_path != expected_receipt:
        raise EvalSealError("receipt_path_mismatch")
    raw = _json_bytes(receipt.model_dump(mode="json"))
    _write_new(receipt_path, raw)
    changed_hashes = dict(entry.receipt_sha256)
    changed_hashes[args.method] = hashlib.sha256(raw).hexdigest()
    changed = entry.model_copy(update={"receipt_sha256": changed_hashes})
    write_ledger(args.state_dir, _replace_case(ledger, changed))
    print(f"score:ok:{case.case_id}:{args.method}:{receipt.disposition}:{seal.source_revision}")
    return 0


def _do_summary(args) -> int:
    seal = verify(args.state_dir)
    cohort, labels, rubric = load_inputs(args.state_dir)
    ledger = load_ledger(args.state_dir)
    receipts = _load_receipts(args.state_dir, ledger, cohort, args.method)
    seal_raw = (args.state_dir / "seal.json").read_bytes()
    summary = summarize(
        cohort,
        labels,
        rubric,
        receipts,
        args.method,
        source_revision=seal.source_revision,
        seal_sha256=hashlib.sha256(seal_raw).hexdigest(),
    )
    output_path = _output_path(args.state_dir, args.output)
    expected = (args.state_dir / "summaries" / f"{args.method}.json").resolve()
    if output_path != expected:
        raise EvalSealError("summary_path_mismatch")
    _write_new(output_path, _json_bytes(summary.model_dump(mode="json")))
    print(f"summary:ok:{args.method}")
    return 0


def _do_compare_summary(args) -> int:
    seal = verify(args.state_dir)
    cohort, labels, rubric = load_inputs(args.state_dir)
    ledger = load_ledger(args.state_dir)
    seal_hash = hashlib.sha256((args.state_dir / "seal.json").read_bytes()).hexdigest()
    summaries = {}
    for method in ("rules", "investigator"):
        path = _safe_path(args.state_dir, Path("summaries") / f"{method}.json", must_exist=True)
        try:
            summary = EvalSummary.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError):
            raise EvalSealError("summary_invalid") from None
        if summary.cohort_id != seal.cohort_id:
            raise EvalSealError("cohort_mismatch")
        if summary.method != method:
            raise EvalSealError("method_mismatch")
        if summary.source_revision != seal.source_revision:
            raise EvalSealError("source_revision_mismatch")
        if summary.seal_sha256 != seal_hash:
            raise EvalSealError("seal_mismatch")
        expected_summary = summarize(
            cohort,
            labels,
            rubric,
            _load_receipts(args.state_dir, ledger, cohort, method),
            method,
            source_revision=seal.source_revision,
            seal_sha256=seal_hash,
        )
        if summary != expected_summary:
            raise EvalSealError("summary_mismatch")
        summaries[method] = summary

    receipts_by_method = {
        method: {
            receipt.case_id: receipt
            for receipt in _load_receipts(args.state_dir, ledger, cohort, method)
        }
        for method in ("rules", "investigator")
    }
    differences = []
    for case in cohort.cases:
        if not {"rules", "investigator"} <= set(case.methods):
            continue
        left = receipts_by_method["rules"][case.case_id]
        right = receipts_by_method["investigator"][case.case_id]
        if left.bundle_sha256 is None or right.bundle_sha256 is None:
            if left.disposition == "missing" or right.disposition == "missing":
                continue
            raise EvalSealError("evidence_mismatch")
        if left.bundle_sha256 != right.bundle_sha256 or left.context_digest != right.context_digest:
            raise EvalSealError("evidence_mismatch")
        left_scores = {item.dimension: item for item in left.dimensions}
        right_scores = {item.dimension: item for item in right.dimensions}
        for dimension in DIMENSIONS:
            a = left_scores[dimension]
            b = right_scores[dimension]
            if a.outcome != b.outcome or a.reasons != b.reasons:
                differences.append(
                    {
                        "case_id": case.case_id,
                        "dimension": dimension,
                        "rules": {"outcome": a.outcome, "reasons": a.reasons},
                        "investigator": {"outcome": b.outcome, "reasons": b.reasons},
                    }
                )
    comparison = {
        "schema_version": VERSIONS["comparison"],
        "cohort_id": seal.cohort_id,
        "source_revision": seal.source_revision,
        "seal_sha256": hashlib.sha256((args.state_dir / "seal.json").read_bytes()).hexdigest(),
        "methods": {
            method: summaries[method].model_dump(mode="json")
            for method in ("rules", "investigator")
        },
        "differences": differences,
    }
    output_path = _output_path(args.state_dir, args.output)
    expected = (args.state_dir / "comparison.json").resolve()
    if output_path != expected:
        raise EvalSealError("comparison_path_mismatch")
    _write_new(output_path, _json_bytes(comparison))
    print("compare-summary:ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "freeze": _do_freeze,
        "verify": _do_verify,
        "compare": _do_compare,
        "score": _do_score,
        "summary": _do_summary,
        "compare-summary": _do_compare_summary,
    }
    try:
        return handlers[args.command](args)
    except (EvalSealError, EvalInputError) as exc:
        print(exc.code)
        return 2
    except ValueError:
        print("input_invalid")
        return 2
    except (OSError, ValidationError, json.JSONDecodeError):
        print("input_invalid")
        return 2
    except Exception:
        print("unexpected_error")
        return 1
