"""Dataset difficulty audit: shortcut baselines, strata and leak scan.

The audit reads one split of a snapshot and needs no model calls.

- P0 predicts the majority gold class of the split.
- T0 is a signature rule over three bundle features: Warning event reasons, an
  ``OOMKilled`` termination in the state, and the maximum error ratio or p95 latency. It
  abstains when the product family matrix marks the matched family ineligible. If T0
  solves a dataset, the dataset cannot rank engine versions.
- A case is ``easy`` when T0 predicts its gold class and ``hard`` otherwise.
- The leak scan reports scenario words in case ids and the bundle source (seen by agents
  that iterate on the split) and lab marker words inside slot payloads (seen by the
  model).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from engine_loop import bundle as raw
from engine_loop import metrics, pipeline
from engine_loop.bundle import RawEvidenceBundle

from cloudops.features.diagnosis.context import family_matrix
from cloudops.features.diagnosis.engine.detector import ERROR_RATIO_HIGH, P95_HIGH_SECONDS
from cloudops.features.evaluation.schemas import EvalLabel

AUDIT_VERSION = "EngineDatasetAudit/v1"
# Words that name a generator scenario or variant. Ids should be opaque.
ID_TOKENS = (
    "crashloop",
    "oom",
    "traffic",
    "pending",
    "imagepull",
    "init-retry",
    "healthy",
    "scope",
    "clean",
    "event-spam",
    "shifted-window",
    "unrelated-restart",
    "noise",
    "inject",
    "nosrc",
)
# Lab markers that real evidence would not contain and that differ between scenarios.
# Kubernetes vocabulary such as OOMKilled, Pending or Unavailable is evidence, not a
# leak. The lab project name appears in every case alike and is not listed either.
CONTENT_PATTERNS = {
    "engine-noise": re.compile(r"engine-noise", re.IGNORECASE),
    "cloudops-unavailable": re.compile(r"cloudops-unavailable", re.IGNORECASE),
    "lab_mode": re.compile(r"\blab_mode\b", re.IGNORECASE),
    "fault": re.compile(r"\bfault\b", re.IGNORECASE),
    "inject": re.compile(r"\binject(ed|ion)?\b", re.IGNORECASE),
    "noise": re.compile(r"\bnoise\b", re.IGNORECASE),
}


def _slots(value: RawEvidenceBundle) -> dict:
    return {item.slot: item for item in value.slots}


def _walk_strings(value, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", str(key)
            yield from _walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _reasons(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "reason" and isinstance(item, str):
                found.add(item)
            found |= _reasons(item)
    elif isinstance(value, list):
        for item in value:
            found |= _reasons(item)
    return found


def _series_max(slot) -> float | None:
    if slot is None or slot.payload is None:
        return None
    values = [
        sample[1]
        for series in slot.payload.get("series") or []
        for sample in series.get("samples") or []
        if len(sample) > 1 and isinstance(sample[1], (int, float))
    ]
    return max(values) if values else None


def features(value: RawEvidenceBundle) -> dict:
    slots = _slots(value)
    events = slots.get("kubernetes.events")
    warnings = sorted(
        {
            item.get("reason")
            for item in ((events.payload or {}).get("events") or [] if events else [])
            if item.get("type") == "Warning" and item.get("reason")
        }
    )
    state = slots.get("kubernetes.state")
    families = family_matrix(raw.materials(value), pipeline.policy())
    return {
        "warning_reasons": warnings,
        "oomkilled": "OOMKilled" in _reasons(state.payload if state else None),
        "max_error_ratio": _series_max(slots.get("prometheus.error_ratio")),
        "max_p95": _series_max(slots.get("prometheus.p95_latency")),
        "eligible": sorted(item.family for item in families if item.eligible_for_assessment),
    }


def _signature(feature: dict) -> str:
    reasons = set(feature["warning_reasons"])
    if "Failed" in reasons:
        return "image_pull"
    if "FailedScheduling" in reasons:
        return "pending_unschedulable"
    if {"BackOff", "Unhealthy"} <= reasons:
        return "oom_restarts" if feature["oomkilled"] else "crashloop_startup"
    if not reasons and (
        (feature["max_error_ratio"] or 0) > ERROR_RATIO_HIGH
        or (feature["max_p95"] or 0) > P95_HIGH_SECONDS
    ):
        return "traffic_latency_errors"
    return "unknown"


def t0(feature: dict) -> str:
    """The signature rule: the first matching signature wins, if its family is eligible."""
    family = _signature(feature)
    return family if family in feature["eligible"] else "unknown"


def leaks(value: RawEvidenceBundle) -> list[dict]:
    found = []
    for where, text in (("case_id", value.case_id), ("source.variant", value.source.variant)):
        for token in ID_TOKENS:
            if text and token in text.lower():
                found.append({"where": where, "token": token})
    for slot in value.slots:
        for path, text in _walk_strings(slot.payload or {}):
            for token, pattern in CONTENT_PATTERNS.items():
                if pattern.search(text):
                    found.append({"where": f"{slot.slot}{path}", "token": token})
    return found


def _accuracy(pairs: list[tuple[str, str]]) -> dict:
    correct = sum(1 for gold, pred in pairs if gold == pred)
    return {
        "k": correct,
        "n": len(pairs),
        "value": round(correct / len(pairs), 4) if pairs else None,
        "wilson95": metrics.wilson(correct, len(pairs)),
    }


def _macro_f1(pairs: list[tuple[str, str]]) -> float | None:
    """Mean F1 over the gold classes present, as in the scorecard."""
    classes = sorted({gold for gold, _ in pairs})
    if not classes:
        return None
    total = 0.0
    for name in classes:
        tp = sum(1 for gold, pred in pairs if gold == name and pred == name)
        fp = sum(1 for gold, pred in pairs if gold != name and pred == name)
        fn = sum(1 for gold, pred in pairs if gold == name and pred != name)
        total += 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    return round(total / len(classes), 4)


def audit(cases: list[tuple[RawEvidenceBundle, EvalLabel]], entries: dict[str, dict]) -> dict:
    """Audit one split. ``entries`` are the manifest case entries keyed by case id."""
    rows = []
    for value, label in cases:
        entry = entries.get(value.case_id, {})
        feature = features(value)
        gold = metrics.gold_class(label)
        pred = t0(feature)
        rows.append(
            {
                "case_id": value.case_id,
                "trial_id": entry.get("trial_id") or value.case_id,
                "derived": str(entry.get("variant", "")).startswith("derived:"),
                "gold": gold,
                "t0": pred,
                "stratum": "easy" if pred == gold else "hard",
                "features": feature,
                "leaks": leaks(value),
            }
        )
    gold_counts = Counter(row["gold"] for row in rows)
    majority = gold_counts.most_common(1)[0][0] if rows else None
    t0_pairs = [(row["gold"], row["t0"]) for row in rows]
    p0_pairs = [(row["gold"], majority) for row in rows]
    per_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pair in t0_pairs:
        per_class[pair[0]].append(pair)
    trials: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        trials[row["trial_id"]].add(row["stratum"])
    signatures: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        signatures[row["gold"]][",".join(row["features"]["warning_reasons"]) or "-"] += 1
    id_leaks = Counter(
        leak["token"]
        for row in rows
        for leak in row["leaks"]
        if leak["where"] in {"case_id", "source.variant"}
    )
    content_leaks = Counter(
        leak["token"]
        for row in rows
        for leak in row["leaks"]
        if leak["where"] not in {"case_id", "source.variant"}
    )
    return {
        "schema_version": AUDIT_VERSION,
        "n_cases": len(rows),
        "n_trials": len(trials),
        "n_derived": sum(1 for row in rows if row["derived"]),
        "gold_classes": dict(sorted(gold_counts.items())),
        "p0": {"predicts": majority, "family_accuracy": _accuracy(p0_pairs)},
        "t0": {
            "family_accuracy": _accuracy(t0_pairs),
            "macro_f1": _macro_f1(t0_pairs),
            "per_class": {name: _accuracy(pairs) for name, pairs in sorted(per_class.items())},
        },
        "strata": {
            "cases": dict(Counter(row["stratum"] for row in rows)),
            "hard_trials": sum(1 for value in trials.values() if "hard" in value),
        },
        "warning_signatures": {
            name: dict(counter.most_common()) for name, counter in sorted(signatures.items())
        },
        "leaks": {
            "cases_with_id_leaks": sum(
                1
                for row in rows
                if any(leak["where"] in {"case_id", "source.variant"} for leak in row["leaks"])
            ),
            "cases_with_content_leaks": sum(
                1
                for row in rows
                if any(leak["where"] not in {"case_id", "source.variant"} for leak in row["leaks"])
            ),
            "id_tokens": dict(id_leaks.most_common()),
            "content_tokens": dict(content_leaks.most_common()),
        },
        "cases": [
            {key: row[key] for key in ("case_id", "trial_id", "gold", "t0", "stratum", "leaks")}
            for row in rows
        ],
    }
