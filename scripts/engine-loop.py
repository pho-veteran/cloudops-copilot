#!/usr/bin/env python3
"""Diagnosis engine improvement loop: snapshots, offline replay, scoring and comparison.

State lives outside Git in ENGINE_LOOP_STATE (default
~/.local/state/cloudops-copilot/engine-loop): datasets/, cache/, runs/, ledger.jsonl.
Results are labelled `dev, non-qualifying`; they never substitute for E06 or E10 evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from engine_loop import dataset, metrics, pipeline
from engine_loop import gateway as gw

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(
    os.environ.get("ENGINE_LOOP_STATE", "~/.local/state/cloudops-copilot/engine-loop")
).expanduser()
LEDGER_KEYS = {
    "id",
    "stage",
    "hypothesis",
    "base_run",
    "head_run",
    "dataset",
    "split",
    "delta",
    "decision",
    "commit",
    "model_calls",
    "tokens",
}


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _commit() -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")


def _run_dir(run: str) -> Path:
    path = Path(run)
    return path if path.is_dir() else STATE / "runs" / run


def _variant(values: list[str]) -> dict[str, str]:
    result = {}
    for item in values:
        key, _, value = item.partition("=")
        if not key or not _:
            raise SystemExit(f"--variant expects key=value, got {item!r}")
        result[key] = value
    return result


def cmd_probe(args) -> int:
    result = asyncio.run(gw.probe())
    _json(STATE / "probes" / f"{_stamp()}.json", result | {"text": result["text"]})
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("kind", "http_status", "served_model", "healthy", "usage", "elapsed_ms")
            }
        )
    )
    return 0 if result["healthy"] else 2


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def cmd_fixture_snapshot(args) -> int:
    out = STATE / "datasets" / args.id
    info = dataset.fixture_snapshot(out, args.id)
    print(json.dumps({"snapshot": str(out), "cases": len(info["cases"])}))
    return 0


def _relabel(value, label, meta):
    """Recompute a lab label with the current coverage rule (a fix in a new snapshot)."""
    from engine_loop import lab

    variant = meta["variant"]
    base = meta.get("kind")
    if (
        variant in {"healthy", "out-of-scope", "derived:source-disabled"}
        or meta.get("target") == "history"
    ):
        kind = None
    else:
        kind = base
    fresh = lab.label(value, kind, rationale=label.rationale[:1900])
    if fresh.model_dump() != label.model_dump():
        fresh = fresh.model_copy(
            update={"rationale": (fresh.rationale + " Relabelled in this snapshot.")[:2000]}
        )
    return fresh


def cmd_snapshot(args) -> int:
    """Freeze lab case batches into one immutable snapshot (split by trial)."""
    from engine_loop import bundle as raw

    from cloudops.features.evaluation.schemas import EvalLabel

    cases = []
    excluded = []
    for source in args.sources:
        folder = Path(source)
        for path in sorted((folder / "bundles").glob("*.json")):
            value = raw.read(path)
            label = EvalLabel.model_validate_json((folder / "labels" / path.name).read_bytes())
            meta = _read(folder / "meta" / path.name)
            if args.relabel:
                label = _relabel(value, label, meta)
            if meta.get("contaminated"):
                excluded.append({"case_id": value.case_id, "reason": "contaminated"})
                continue
            split = dataset.split_for(meta["trial_id"])
            cases.append(
                (
                    value,
                    label,
                    split,
                    {
                        "family": label.category,
                        "variant": meta["variant"],
                        "kind": meta.get("kind"),
                        "trial_id": meta["trial_id"],
                        "source": folder.name,
                    },
                )
            )
    pilot = []
    dev = sorted((item for item in cases if item[2] == "dev"), key=lambda item: item[0].case_id)
    for family in dataset.FAMILIES:
        found = next(
            (
                item[0].case_id
                for item in dev
                if item[1].category == family and item[3]["variant"] == "clean"
            ),
            None,
        )
        if found:
            pilot.append(found)
    for variant in ("healthy", "derived:injection-logs"):
        found = next(
            (
                item[0].case_id
                for item in dev
                if item[3]["variant"] == variant and item[1].required_result == "abstention"
            ),
            None,
        ) or next((item[0].case_id for item in dev if item[3]["variant"] == variant), None)
        if found:
            pilot.append(found)
    out = STATE / "datasets" / args.id
    info = dataset.write_snapshot(
        out,
        snapshot_id=args.id,
        cases=cases,
        description=args.description,
        pilot=pilot,
    )
    summary = {
        "snapshot": str(out),
        "cases": len(info["cases"]),
        "excluded": excluded,
        "splits": {
            split: sum(1 for item in cases if item[2] == split) for split in ("dev", "test")
        },
        "pilot": pilot,
    }
    print(json.dumps(summary, indent=2))
    return 0


def _bundle_slots(value) -> dict[str, str]:
    return {item.slot: item.coverage_status for item in value.slots}


async def _replay(args, cases, settings, run_dir: Path, base_scores: dict | None) -> list[dict]:
    runner, needs_model = pipeline.CONFIGS[args.config]
    gateway = None
    if needs_model:
        gw.check_environment(os.environ)
        gateway = gw.Gateway(
            cache_dir=STATE / "cache",
            model_profile=gw.profile(max_input_bytes=settings.budget),
            concurrency=args.concurrency,
            calls_log=run_dir / "calls.jsonl",
        )
    labels = {value.case_id: label for value, label in cases}
    regressions = 0
    records: list[dict] = []
    stop: str | None = None

    async def one(value):
        record = await runner(value, settings=settings, gateway=gateway)
        record = {"case_id": value.case_id, "bundle_slots": _bundle_slots(value), **record}
        _json(run_dir / "cases" / f"{value.case_id}.json", record)
        return record

    tasks = [asyncio.create_task(one(value)) for value, _ in cases]
    try:
        for task in asyncio.as_completed(tasks):
            record = await task
            records.append(record)
            if base_scores is not None and args.pilot:
                score = metrics.case_score(record, labels[record["case_id"]])
                before = base_scores.get(record["case_id"])
                if before and before["correct"] and not score["correct"]:
                    regressions += 1
                    if regressions >= 2:
                        stop = "pilot aborted after 2 regressions"
                        break
    except gw.PolicyStop as error:
        stop = str(error)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if stop:
        _json(run_dir / "stopped.json", {"reason": stop, "at": _stamp()})
        print(json.dumps({"stopped": stop}), file=sys.stderr)
    if gateway is not None:
        _json(
            run_dir / "gateway.json",
            {"sent": gateway.sent, "cache_hits": gateway.cache_hits, "stopped": gateway.stopped},
        )
    return records


def cmd_replay(args) -> int:
    snapshot = STATE / "datasets" / args.dataset
    if args.split == "test" and not args.milestone:
        raise SystemExit("The test split is sealed; pass --milestone only for a milestone report.")
    info = dataset.manifest(snapshot)
    wanted = info["pilot"] if args.pilot else (args.cases.split(",") if args.cases else None)
    cases = dataset.load(snapshot, args.split, milestone=args.milestone, cases=wanted)
    settings = pipeline.Settings(
        budget=args.budget,
        repair=not (args.pilot or args.no_repair),
        variant=_variant(args.variant),
    )
    run_id = args.run_id or "-".join(
        [_stamp(), args.config.lower(), args.dataset, args.split, "pilot" if args.pilot else "full"]
    )
    run_dir = STATE / "runs" / run_id
    if run_dir.exists():
        raise SystemExit(f"run exists: {run_dir}")
    config = {
        "run_id": run_id,
        "config": args.config,
        "dataset": args.dataset,
        "dataset_manifest_sha256": dataset.snapshot_digest(snapshot),
        "split": args.split,
        "pilot": args.pilot,
        "cases": [value.case_id for value, _ in cases],
        "settings": {
            "budget": settings.budget,
            "repair": settings.repair,
            "variant": settings.variant,
            "max_tokens": gw.MAX_TOKENS,
            "temperature": "not sent (product profile has no temperature field)",
            "concurrency": args.concurrency,
        },
        "model": gw.MODEL if pipeline.CONFIGS[args.config][1] else None,
        "commit": _commit(),
        "started_at": _stamp(),
        "label": "dev, non-qualifying",
    }
    _json(run_dir / "config.json", config)
    base_scores = None
    if args.base:
        base_scores = {item["case_id"]: item for item in _read(_run_dir(args.base) / "scores.json")}
    asyncio.run(_replay(args, cases, settings, run_dir, base_scores))
    card = _score(run_dir)
    summary = {
        key: card[key]
        for key in ("n", "macro_f1", "overflow", "hallucinated_aliases", "calls_sent_this_run")
    }
    summary["family_accuracy"] = card["family_accuracy"]["value"]
    print(json.dumps({"run": run_id, **summary}))
    return 0


def _score(run_dir: Path) -> dict:
    config = _read(run_dir / "config.json")
    snapshot = STATE / "datasets" / config["dataset"]
    split = config["split"]
    label_path = snapshot / "labels" / f"{split}.json"
    from cloudops.features.evaluation.schemas import EvalLabels

    labels = EvalLabels.model_validate_json(label_path.read_bytes()).cases
    records = [_read(path) for path in sorted((run_dir / "cases").glob("*.json"))]
    scores = [metrics.case_score(record, labels[record["case_id"]]) for record in records]
    card = metrics.scorecard(scores)
    card["run_id"] = config["run_id"]
    card["config"] = config["config"]
    card["dataset"] = config["dataset"]
    card["split"] = split
    card["planned_cases"] = len(config["cases"])
    _json(run_dir / "scores.json", scores)
    _json(run_dir / "scorecard.json", card)
    _json(run_dir / "triage.json", metrics.triage(scores))
    return card


def cmd_score(args) -> int:
    card = _score(_run_dir(args.run))
    print(json.dumps(card, indent=2))
    return 0


def cmd_triage(args) -> int:
    run_dir = _run_dir(args.run)
    if not (run_dir / "triage.json").exists():
        _score(run_dir)
    print(json.dumps(_read(run_dir / "triage.json"), indent=2))
    return 0


def cmd_compare(args) -> int:
    base, head = _run_dir(args.base), _run_dir(args.head)
    for path in (base, head):
        if not (path / "scores.json").exists():
            _score(path)
    result = metrics.compare(
        _read(base / "scores.json"),
        _read(head / "scores.json"),
        _read(base / "scorecard.json"),
        _read(head / "scorecard.json"),
        args.target,
    )
    result["base_run"] = base.name
    result["head_run"] = head.name
    _json(head / f"compare-{base.name}.json", result)
    print(json.dumps(result, indent=2))
    return 0


def cmd_report(args) -> int:
    """Milestone report: scorecards with intervals, paired McNemar and noise robustness."""

    lines = [f"# {args.title}", ""]
    if args.preamble:
        lines += [Path(args.preamble).read_text(encoding="utf-8").strip(), ""]
    runs = [_run_dir(item) for item in args.run]
    cards = {}
    for path in runs:
        if not (path / "scorecard.json").exists():
            _score(path)
        cards[path.name] = (_read(path / "config.json"), _read(path / "scorecard.json"))
    lines += [
        "## Runs",
        "",
        "| Run | Config | Dataset | Split | n | Commit | Model |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for name, (config, card) in cards.items():
        lines.append(
            f"| `{name}` | {config['config']} {config['settings'].get('variant') or ''} | "
            f"`{config['dataset']}` (`{config['dataset_manifest_sha256'][:12]}`) | "
            f"{config['split']} | {card['n']} | `{config['commit'][:12]}` | "
            f"{config.get('model') or '-'} |"
        )

    def rate(item):
        if not isinstance(item, dict) or item.get("value") is None:
            return "-"
        interval = item.get("wilson95") or ["?", "?"]
        return f"{item['value']:.3f} ({item['k']}/{item['n']}; {interval[0]:.2f}-{interval[1]:.2f})"

    lines += [
        "",
        "## Scorecards",
        "",
        "| Metric | " + " | ".join(f"`{name}`" for name in cards) + " |",
        "| --- |" + " --- |" * len(cards),
    ]
    rows = [
        ("Family accuracy", lambda c: rate(c["family_accuracy"])),
        ("Macro-F1", lambda c: f"{c['macro_f1']:.3f}"),
        ("Negatives false positive", lambda c: rate(c["negatives_fp"])),
        ("Abstention precision", lambda c: rate(c["abstention_precision"])),
        ("Abstention recall", lambda c: rate(c["abstention_recall"])),
        ("Citation precision", lambda c: rate(c["citation_precision"])),
        ("Citation recall", lambda c: rate(c["citation_recall"])),
        ("Hallucinated aliases", lambda c: str(c["hallucinated_aliases"])),
        ("First-attempt valid", lambda c: rate(c["first_attempt_valid"])),
        ("Plan correct", lambda c: rate(c["plan_correct"])),
        ("Manual-only correct", lambda c: rate(c["manual_only_correct"])),
        ("Required-source recall (sent)", lambda c: rate(c["required_source_recall_sent"])),
        (
            "Message bytes p50 / max",
            lambda c: f"{c['message_bytes']['p50']} / {c['message_bytes']['max']}",
        ),
        ("Overflow at 65,536", lambda c: str(c["overflow"])),
        (
            "Latency p50 / p95 (ms)",
            lambda c: f"{c['latency_ms']['p50']} / {c['latency_ms']['p95']}",
        ),
        ("Reasoning tokens p50", lambda c: str(c["reasoning_tokens_per_case"]["p50"])),
        (
            "Failure tags",
            lambda c: ", ".join(f"{k} {v}" for k, v in sorted(c["tags"].items())) or "-",
        ),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(card) for _, card in cards.values()) + " |")
    for pair in args.pair:
        base, head = (_run_dir(item) for item in pair.split(":"))
        result = metrics.compare(
            _read(base / "scores.json"),
            _read(head / "scores.json"),
            _read(base / "scorecard.json"),
            _read(head / "scorecard.json"),
            None,
        )
        test = result["mcnemar"]
        lines += [
            "",
            f"## Paired comparison `{base.name}` -> `{head.name}`",
            "",
            f"n paired = {result['n_paired']}; base-only correct = {test['base_only_correct']}; "
            f"head-only correct = {test['head_only_correct']}; exact McNemar p = {test['p_value']}.",
        ]
    if args.robustness:
        path = _run_dir(args.robustness)
        config = _read(path / "config.json")
        scores = {item["case_id"]: item for item in _read(path / "scores.json")}
        snapshot = STATE / "datasets" / config["dataset"]
        groups: dict[str, list[bool]] = {}
        for value, _ in dataset.load(
            snapshot, config["split"], milestone=config["split"] == "test"
        ):
            variant = value.source.variant or "fixture"
            clean = variant == "clean"
            score = scores.get(value.case_id)
            if score is None or score["gold"] == "unknown":
                continue
            groups.setdefault("clean" if clean else "noise", []).append(score["correct"])
        lines += ["", f"## Noise robustness (`{path.name}`, positive labels)", ""]
        for name, values in sorted(groups.items()):
            lines.append(
                f"- {name}: {sum(values)}/{len(values)} correct "
                f"({sum(values) / len(values):.3f}, Wilson {metrics.wilson(sum(values), len(values))})"
            )
        if "clean" in groups and "noise" in groups:
            drop = sum(groups["clean"]) / len(groups["clean"]) - sum(groups["noise"]) / len(
                groups["noise"]
            )
            lines.append(f"- accuracy drop clean -> noise: {drop * 100:.1f} points")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(out)}))
    return 0


def cmd_ledger(args) -> int:
    entry = json.loads(Path(args.entry).read_text(encoding="utf-8"))
    missing = LEDGER_KEYS - set(entry)
    if missing:
        raise SystemExit(f"ledger entry misses {sorted(missing)}")
    entry.setdefault("date", datetime.now(timezone.utc).isoformat())
    path = STATE / "ledger.jsonl"
    existing = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if any(item.get("id") == entry["id"] for item in existing):
        raise SystemExit(f"ledger id exists: {entry['id']}")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    print(json.dumps({"appended": entry["id"]}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe")
    fixture = sub.add_parser("fixture-snapshot")
    fixture.add_argument("--id", default="e06-fixtures-v1")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--id", required=True)
    snap.add_argument("--description", required=True)
    snap.add_argument("--relabel", action="store_true", help="Apply the current label rule.")
    snap.add_argument("sources", nargs="+", help="Lab case batch directories.")
    replay = sub.add_parser("replay")
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--split", choices=dataset.SPLITS, default="dev")
    replay.add_argument("--config", choices=sorted(pipeline.CONFIGS), required=True)
    replay.add_argument("--variant", action="append", default=[])
    replay.add_argument("--concurrency", type=int, default=gw.MAX_CONCURRENCY)
    replay.add_argument("--budget", type=int, default=65536)
    replay.add_argument("--pilot", action="store_true")
    replay.add_argument("--no-repair", action="store_true")
    replay.add_argument("--cases")
    replay.add_argument("--base", help="Base run for pilot regression abort.")
    replay.add_argument("--run-id")
    replay.add_argument("--milestone", action="store_true")
    for name in ("score", "triage"):
        command = sub.add_parser(name)
        command.add_argument("--run", required=True)
    comparison = sub.add_parser("compare")
    comparison.add_argument("--base", required=True)
    comparison.add_argument("--head", required=True)
    comparison.add_argument("--target")
    report = sub.add_parser("report")
    report.add_argument("--title", required=True)
    report.add_argument("--out", required=True)
    report.add_argument("--run", action="append", required=True)
    report.add_argument("--pair", action="append", default=[], help="BASE:HEAD")
    report.add_argument("--robustness", help="Run whose split is grouped by variant.")
    report.add_argument("--preamble", help="Markdown file inserted after the title.")
    ledger = sub.add_parser("ledger")
    ledger.add_argument("--entry", required=True, help="JSON file with one ledger entry.")
    args = parser.parse_args()
    handlers = {
        "probe": cmd_probe,
        "fixture-snapshot": cmd_fixture_snapshot,
        "snapshot": cmd_snapshot,
        "replay": cmd_replay,
        "score": cmd_score,
        "triage": cmd_triage,
        "compare": cmd_compare,
        "ledger": cmd_ledger,
        "report": cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
