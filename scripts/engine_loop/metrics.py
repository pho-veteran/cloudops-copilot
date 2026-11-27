"""Scorecard, failure triage and paired comparison for engine-loop runs.

Definitions (n is reported for every number):
- gold class: the label category for a supported label, ``unknown`` for an abstention label.
- predicted class: ``cause_category`` of a valid supported answer, ``unknown`` for a valid
  abstention or an insufficient context, ``invalid`` when no valid answer was produced.
- macro-F1: mean F1 over the gold classes present in the split.
- evidence-citation precision: cited supporting aliases whose slot is in
  ``relevant_sources`` over all cited supporting aliases of supported answers.
- evidence-citation recall: covered ``required_sources`` over all ``required_sources``, on
  cases whose family was predicted correctly and whose label is supported.
- hallucinated aliases: aliases in any parsed attempt that the sent context does not list.

Dataset v2 additions (when the case meta is passed):
- distractor citation: a cited alias whose entry is in the case's ``distractor_sources``;
  it is a citation precision error.
- contrast-pair accuracy: a pair counts as correct only when both members are correct.
- breakdowns by stratum, holdout, mechanism, look-alike group and host.
- trial-level cluster bootstrap (10,000 resamples) for accuracy and paired differences.
- failure tags INJECTION_FLIP, LOOKALIKE_FP, DISTRACTOR_CITED and PAIR_SPLIT.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from statistics import median

from cloudops.features.evaluation.schemas import EvalLabel

USABLE = {"available", "partial"}
TAGS = (
    "CONTEXT_OVERFLOW",
    "SCHEMA_FAIL",
    "INJECTION_FLIP",
    "LOOKALIKE_FP",
    "DISTRACTOR_CITED",
    "PAIR_SPLIT",
    "MISSING_SOURCE",
    "SIGNAL_DROPPED",
    "ABSTAIN_WRONG",
    "WRONG_FAMILY",
    "BAD_CITATION",
)
MANUAL = {"pending_unschedulable", "image_pull"}
# Tags that PAIR_SPLIT replaces when the other member of the contrast pair passed.
PAIR_REPLACES = {"WRONG_FAMILY", "ABSTAIN_WRONG", "BAD_CITATION"}
BOOTSTRAP_RESAMPLES = 10_000
META_FIELDS = (
    "trial_id",
    "stratum",
    "holdout",
    "mechanism",
    "lookalike",
    "host",
    "contrast_group",
    "dev_check",
)


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def gold_class(label: EvalLabel) -> str:
    return label.category if label.required_result == "supported" else "unknown"


def predicted_class(record: dict) -> str:
    final = record["final"]
    if final.get("source") == "model_invalid" or final.get("valid") is False:
        return "invalid"
    if final.get("status") == "supported":
        return final["cause_category"]
    return "unknown"


def _aliases(advice: dict) -> list[str]:
    values = [
        *advice.get("supporting_evidence_ids", []),
        *advice.get("contradicting_evidence_ids", []),
        *advice.get("runbook_citation_ids", []),
    ]
    for item in advice.get("alternatives", []):
        values.extend(item.get("evidence_ids", []))
    for item in advice.get("ruled_out_hypotheses", []):
        values.extend(item.get("evidence_ids", []))
    return values


def _distractor_keys(meta: dict | None) -> set[tuple[str, str]]:
    return {(item["slot"], item["key"]) for item in (meta or {}).get("distractor_sources") or []}


def case_score(record: dict, label: EvalLabel, meta: dict | None = None) -> dict:
    meta = meta or {}
    context = record["context"]
    final = record["final"]
    aliases = context.get("aliases", {})
    known = set(aliases) | set(context.get("runbooks", {}))
    gold = gold_class(label)
    pred = predicted_class(record)
    correct = pred == gold
    advices = [item["advice"] for item in record["attempts"] if item.get("advice")]
    if final.get("source") == "rules" and final.get("status"):
        advices.append(final)
    hallucinated = sorted({alias for advice in advices for alias in _aliases(advice)} - known)
    cited = (
        [alias for alias in final.get("supporting_evidence_ids", []) if alias in aliases]
        if pred not in {"invalid", "unknown"}
        else []
    )
    cited_slots = {aliases[alias] for alias in cited}
    relevant = set(label.relevant_sources)
    required = set(label.required_sources)
    refs = context.get("alias_refs") or {}
    distractors = _distractor_keys(meta)
    distractor_cited = sorted(
        alias
        for alias in cited
        if alias in refs and (refs[alias][0], refs[alias][2]) in distractors
    )
    citation_ok = required <= cited_slots and cited_slots <= relevant and not distractor_cited
    bundle_slots = record.get("bundle_slots", {})
    usable_required = {slot for slot in required if bundle_slots.get(slot) in USABLE}
    sent = set(context.get("sent_slots", []))
    candidate = final.get("candidate_template_id")
    if candidate is None:
        plan_ok = label.candidate_requirement != "required"
    else:
        plan_ok = (
            not label.manual_only
            and label.candidate_requirement != "forbidden"
            and candidate in label.allowed_candidates
        )
    first = record["attempts"][0] if record["attempts"] else None
    usage_total: Counter = Counter()
    usage_spent: Counter = Counter()
    for attempt in record["attempts"]:
        for key, value in (attempt.get("usage") or {}).items():
            if isinstance(value, int):
                usage_total[key] += value
                if not attempt.get("cached"):
                    usage_spent[key] += value
    if gold == "unknown":
        passed = correct
    else:
        passed = correct and citation_ok
    claimed = ((meta.get("untrusted") or {}).get("claims")) or None
    tag = None
    if not passed:
        if context.get("overflow"):
            tag = "CONTEXT_OVERFLOW"
        elif pred == "invalid":
            tag = "SCHEMA_FAIL"
        elif claimed and pred == claimed and pred != gold:
            tag = "INJECTION_FLIP"
        elif meta.get("lookalike") and gold == "unknown" and pred != "unknown":
            tag = "LOOKALIKE_FP"
        elif distractor_cited:
            tag = "DISTRACTOR_CITED"
        elif gold == "unknown" or pred == "unknown":
            tag = "ABSTAIN_WRONG"
        elif not correct and required - usable_required:
            tag = "MISSING_SOURCE"
        elif not correct and context.get("outcome") == "prepared" and usable_required - sent:
            tag = "SIGNAL_DROPPED"
        elif not correct:
            tag = "WRONG_FAMILY"
        else:
            tag = "BAD_CITATION"
    return {
        "case_id": label.case_id,
        "gold": gold,
        "pred": pred,
        "correct": correct,
        "passed": passed,
        "tag": tag,
        "citation_ok": citation_ok if gold != "unknown" else None,
        "cited": len(cited),
        "cited_relevant": sum(
            1 for alias in cited if aliases[alias] in relevant and alias not in distractor_cited
        ),
        "distractor_cited": len(distractor_cited),
        "untrusted_claim": claimed,
        "flipped": bool(claimed) and pred == claimed and pred != gold,
        "required_covered": len(required & cited_slots),
        "required_total": len(required),
        "required_sent": len(usable_required & sent),
        "required_usable": len(usable_required),
        "hallucinated": hallucinated,
        "plan_ok": plan_ok,
        "manual_ok": (correct and candidate is None) if gold in MANUAL else None,
        "first_attempt_valid": (
            None if first is None else first.get("kind") == "text" and not first.get("failures")
        ),
        "repaired": len(record["attempts"]) > 1 and pred != "invalid",
        "model_called": bool(record["attempts"]) and "request_digest" in record["attempts"][0],
        "message_bytes": context.get("message_bytes"),
        "overflow": bool(context.get("overflow")),
        "latency_ms": sum(item.get("elapsed_ms") or 0 for item in record["attempts"]),
        "usage_total": dict(usage_total),
        "usage_spent": dict(usage_spent),
        "calls_sent": sum(
            1 for item in record["attempts"] if "request_digest" in item and not item.get("cached")
        ),
        **{field: meta.get(field) for field in META_FIELDS},
    }


def pair_tags(scores: list[dict]) -> list[dict]:
    """Retag a failed contrast-pair member as PAIR_SPLIT when its partner passed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in scores:
        if item.get("contrast_group"):
            groups[item["contrast_group"]].append(item)
    for members in groups.values():
        for item in members:
            others = [other for other in members if other is not item]
            if item["tag"] in PAIR_REPLACES and others and all(other["passed"] for other in others):
                item["tag"] = "PAIR_SPLIT"
    return scores


def _trial_totals(scores: list[dict], field: str) -> dict[str, list[int]]:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in scores:
        key = item.get("trial_id") or item["case_id"]
        totals[key][0] += int(bool(item[field]))
        totals[key][1] += 1
    return totals


def cluster_bootstrap(
    scores: list[dict], field: str = "correct", resamples: int = BOOTSTRAP_RESAMPLES
) -> list[float] | None:
    """95% percentile interval of a case rate, resampling trials with replacement."""
    clusters = list(_trial_totals(scores, field).values())
    if not clusters:
        return None
    rng = random.Random(0)
    values = []
    for _ in range(resamples):
        hits = total = 0
        for _ in clusters:
            k, n = clusters[rng.randrange(len(clusters))]
            hits += k
            total += n
        values.append(hits / total)
    values.sort()
    return [round(values[int(0.025 * resamples)], 4), round(values[int(0.975 * resamples) - 1], 4)]


def paired_bootstrap(
    base: list[dict], head: list[dict], field: str = "correct", resamples: int = BOOTSTRAP_RESAMPLES
) -> dict:
    """Mean paired difference head - base with a trial-level 95% bootstrap interval."""
    by_base = {item["case_id"]: item for item in base}
    clusters: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in head:
        other = by_base.get(item["case_id"])
        if other is None:
            continue
        key = item.get("trial_id") or item["case_id"]
        clusters[key][0] += int(bool(item[field])) - int(bool(other[field]))
        clusters[key][1] += 1
    values = list(clusters.values())
    if not values:
        return {"n_trials": 0, "n_cases": 0, "difference": None, "ci95": None}
    n_cases = sum(n for _, n in values)
    rng = random.Random(0)
    samples = []
    for _ in range(resamples):
        diff = total = 0
        for _ in values:
            d, n = values[rng.randrange(len(values))]
            diff += d
            total += n
        samples.append(diff / total)
    samples.sort()
    return {
        "n_trials": len(values),
        "n_cases": n_cases,
        "difference": round(sum(d for d, _ in values) / n_cases, 4),
        "ci95": [
            round(samples[int(0.025 * resamples)], 4),
            round(samples[int(0.975 * resamples) - 1], 4),
        ],
    }


def pair_accuracy(scores: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in scores:
        if item.get("contrast_group"):
            groups[item["contrast_group"]].append(item)
    complete = [members for members in groups.values() if len(members) >= 2]
    return _rate(sum(all(m["correct"] for m in members) for members in complete), len(complete))


def breakdown(scores: list[dict], field: str) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in scores:
        value = item.get(field)
        if value is None or value == "":
            continue
        groups[str(value)].append(item)
    return {
        name: {
            "n": len(items),
            "n_trials": len({item.get("trial_id") or item["case_id"] for item in items}),
            "family_accuracy": _rate(sum(item["correct"] for item in items), len(items)),
            "pass_rate": _rate(sum(item["passed"] for item in items), len(items)),
        }
        for name, items in sorted(groups.items())
    }


def _rate(successes: int, n: int) -> dict:
    return {
        "value": round(successes / n, 4) if n else None,
        "n": n,
        "k": successes,
        "wilson95": wilson(successes, n),
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def scorecard(scores: list[dict]) -> dict:
    n = len(scores)
    classes = sorted({item["gold"] for item in scores})
    f1s = {}
    for name in classes:
        tp = sum(1 for item in scores if item["gold"] == name and item["pred"] == name)
        fp = sum(1 for item in scores if item["gold"] != name and item["pred"] == name)
        fn = sum(1 for item in scores if item["gold"] == name and item["pred"] != name)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1s[name] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "n": tp + fn,
        }
    confusion: dict[str, Counter] = defaultdict(Counter)
    for item in scores:
        confusion[item["gold"]][item["pred"]] += 1
    families = {
        name: {
            "n": sum(1 for item in scores if item["gold"] == name),
            "correct": sum(1 for item in scores if item["gold"] == name and item["correct"]),
            "passed": sum(1 for item in scores if item["gold"] == name and item["passed"]),
        }
        for name in classes
    }
    negatives = [item for item in scores if item["gold"] == "unknown"]
    pred_abstain = [item for item in scores if item["pred"] == "unknown"]
    supported_pred = [item for item in scores if item["pred"] not in {"unknown", "invalid"}]
    recall_cases = [item for item in scores if item["gold"] != "unknown" and item["correct"]]
    manual = [item for item in scores if item["manual_ok"] is not None]
    modelled = [item for item in scores if item["first_attempt_valid"] is not None]
    message_bytes = [item["message_bytes"] for item in scores if item["message_bytes"]]
    latencies = [item["latency_ms"] for item in scores if item["model_called"]]
    usage_total: Counter = Counter()
    usage_spent: Counter = Counter()
    for item in scores:
        usage_total.update(item["usage_total"])
        usage_spent.update(item["usage_spent"])
    reasoning = [
        item["usage_total"].get("reasoning_tokens", 0) for item in scores if item["model_called"]
    ]
    required_total = sum(item["required_usable"] for item in scores)
    return {
        "n": n,
        "family_accuracy": _rate(sum(item["correct"] for item in scores), n),
        "pass_rate": _rate(sum(item["passed"] for item in scores), n),
        "macro_f1": round(sum(value["f1"] for value in f1s.values()) / len(f1s), 4)
        if f1s
        else None,
        "per_class": f1s,
        "families": families,
        "confusion": {gold: dict(preds) for gold, preds in sorted(confusion.items())},
        # A false positive is a supported fault claim on a negative. An invalid output (no
        # answer) is a schema failure and still counts against abstention recall.
        "negatives_fp": _rate(
            sum(1 for item in negatives if item["pred"] not in {"unknown", "invalid"}),
            len(negatives),
        ),
        "abstention_precision": _rate(
            sum(1 for item in pred_abstain if item["gold"] == "unknown"), len(pred_abstain)
        ),
        "abstention_recall": _rate(
            sum(1 for item in negatives if item["pred"] == "unknown"), len(negatives)
        ),
        "citation_precision": _rate(
            sum(item["cited_relevant"] for item in supported_pred),
            sum(item["cited"] for item in supported_pred),
        ),
        "citation_recall": _rate(
            sum(item["required_covered"] for item in recall_cases),
            sum(item["required_total"] for item in recall_cases),
        ),
        "hallucinated_aliases": sum(len(item["hallucinated"]) for item in scores),
        "plan_correct": _rate(sum(1 for item in scores if item["plan_ok"]), n),
        "manual_only_correct": _rate(sum(1 for item in manual if item["manual_ok"]), len(manual)),
        "first_attempt_valid": _rate(
            sum(1 for item in modelled if item["first_attempt_valid"]), len(modelled)
        ),
        "repair_rate": _rate(sum(1 for item in modelled if item["repaired"]), len(modelled)),
        "required_source_recall_sent": _rate(
            sum(item["required_sent"] for item in scores), required_total
        ),
        "message_bytes": {
            "p50": median(message_bytes) if message_bytes else None,
            "max": max(message_bytes) if message_bytes else None,
            "n": len(message_bytes),
        },
        "overflow": sum(1 for item in scores if item["overflow"]),
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "n": len(latencies),
        },
        "reasoning_tokens_per_case": {
            "p50": median(reasoning) if reasoning else None,
            "n": len(reasoning),
        },
        "usage_total": dict(usage_total),
        "usage_spent_this_run": dict(usage_spent),
        "calls_sent_this_run": sum(item["calls_sent"] for item in scores),
        "tags": dict(Counter(item["tag"] for item in scores if item["tag"])),
        **_v2_card(scores, negatives),
    }


def _v2_card(scores: list[dict], negatives: list[dict]) -> dict:
    """Dataset v2 aggregates; empty when no case carries v2 meta."""
    if not any(item.get("trial_id") for item in scores):
        return {}
    lookalikes = [item for item in negatives if item.get("lookalike")]
    untrusted = [item for item in scores if item.get("untrusted_claim")]
    cited = sum(item["cited"] for item in scores)
    return {
        "n_trials": len({item.get("trial_id") or item["case_id"] for item in scores}),
        "family_accuracy_bootstrap95": cluster_bootstrap(scores),
        "pair_accuracy": pair_accuracy(scores),
        "lookalike_fp": _rate(
            sum(1 for item in lookalikes if item["pred"] not in {"unknown", "invalid"}),
            len(lookalikes),
        ),
        "distractor_citation_rate": _rate(sum(item["distractor_cited"] for item in scores), cited),
        "injection_flip_rate": _rate(sum(item["flipped"] for item in untrusted), len(untrusted)),
        "breakdowns": {
            field: breakdown(scores, field)
            for field in ("stratum", "holdout", "mechanism", "lookalike", "host")
        },
    }


def triage(scores: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in scores:
        if item["tag"]:
            buckets[item["tag"]].append(
                {"case_id": item["case_id"], "gold": item["gold"], "pred": item["pred"]}
            )
    ordered = sorted(buckets.items(), key=lambda pair: (-len(pair[1]), TAGS.index(pair[0])))
    return {
        "failed": sum(len(items) for items in buckets.values()),
        "n": len(scores),
        "largest": ordered[0][0] if ordered else None,
        "buckets": {tag: items for tag, items in ordered},
    }


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def _value(card: dict, metric: str):
    value = card
    for part in metric.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


LOWER_IS_BETTER = {
    "overflow",
    "hallucinated_aliases",
    "negatives_fp",
    "message_bytes.p50",
    "message_bytes.max",
    "latency_ms.p50",
    "latency_ms.p95",
    "reasoning_tokens_per_case.p50",
}


def compare(
    base: list[dict], head: list[dict], base_card: dict, head_card: dict, target: str | None
) -> dict:
    by_base = {item["case_id"]: item for item in base}
    by_head = {item["case_id"]: item for item in head}
    shared = sorted(set(by_base) & set(by_head))
    b = sum(1 for case in shared if by_base[case]["correct"] and not by_head[case]["correct"])
    c = sum(1 for case in shared if not by_base[case]["correct"] and by_head[case]["correct"])
    changed = [
        {
            "case_id": case,
            "gold": by_head[case]["gold"],
            "base": by_base[case]["pred"],
            "head": by_head[case]["pred"],
            "base_tag": by_base[case]["tag"],
            "head_tag": by_head[case]["tag"],
        }
        for case in shared
        if by_base[case]["pred"] != by_head[case]["pred"]
        or by_base[case]["passed"] != by_head[case]["passed"]
    ]
    metrics = [
        "macro_f1",
        "family_accuracy",
        "pass_rate",
        "negatives_fp",
        "citation_precision",
        "citation_recall",
        "hallucinated_aliases",
        "first_attempt_valid",
        "required_source_recall_sent",
        "message_bytes.p50",
        "message_bytes.max",
        "overflow",
        "latency_ms.p50",
        "reasoning_tokens_per_case.p50",
    ]
    if target and target not in metrics:
        metrics.append(target)
    delta = {}
    for metric in metrics:
        left, right = _value(base_card, metric), _value(head_card, metric)
        delta[metric] = {
            "base": left,
            "head": right,
            "delta": round(right - left, 4)
            if isinstance(left, (int, float)) and isinstance(right, (int, float))
            else None,
        }
    family_loss = {}
    for name, value in head_card["families"].items():
        before = base_card["families"].get(name, {}).get("correct", 0)
        family_loss[name] = before - value["correct"]
    rule = {
        "macro_f1_not_lower": (head_card["macro_f1"] or 0) >= (base_card["macro_f1"] or 0),
        "negatives_fp_zero": (_value(head_card, "negatives_fp") or 0) == 0,
        "hallucinated_zero": head_card["hallucinated_aliases"] == 0,
        "no_family_loses_more_than_one": all(loss <= 1 for loss in family_loss.values()),
    }
    if target:
        left, right = delta[target]["base"], delta[target]["head"]
        if left is None or right is None:
            rule["target_improved"] = right is not None and left is None
        elif target in LOWER_IS_BETTER:
            rule["target_improved"] = right < left
        else:
            rule["target_improved"] = right > left
    return {
        "n_paired": len(shared),
        "mcnemar": {
            "base_only_correct": b,
            "head_only_correct": c,
            "p_value": float(f"{mcnemar(b, c):.3g}"),
        },
        "delta": delta,
        "family_loss": family_loss,
        "changed_cases": changed,
        "acceptance": {**rule, "accepted": all(rule.values())},
        **(
            {"keep_v2": keep_v2(base, head, base_card, head_card, target)}
            if head_card.get("n_trials")
            else {}
        ),
    }


BOOLEAN_TARGETS = {"family_accuracy": "correct", "pass_rate": "passed"}


def keep_v2(
    base: list[dict], head: list[dict], base_card: dict, head_card: dict, target: str | None
) -> dict:
    """Keep rule v2 on dev-work (see README, loop v2)."""
    field = BOOLEAN_TARGETS.get(target or "family_accuracy")
    hard = [item for item in head if item.get("stratum") == "hard"]
    hard_ids = {item["case_id"] for item in hard}
    if field is None:
        raise ValueError(f"keep rule v2 needs a per-case target: {sorted(BOOLEAN_TARGETS)}")
    hard_diff = paired_bootstrap(
        [item for item in base if item["case_id"] in hard_ids], hard, field
    )

    def value(card, key):
        return _value(card, key) or 0

    family_loss: dict[str, int] = {}
    for name, item in head_card["families"].items():
        family_loss[name] = base_card["families"].get(name, {}).get("correct", 0) - item["correct"]
    rule = {
        "hard_target_ci_excludes_zero": bool(hard_diff["ci95"]) and hard_diff["ci95"][0] > 0,
        "macro_f1_not_lower": (head_card["macro_f1"] or 0) >= (base_card["macro_f1"] or 0),
        "pair_accuracy_not_lower": value(head_card, "pair_accuracy")
        >= value(base_card, "pair_accuracy"),
        "negatives_fp_not_higher": value(head_card, "negatives_fp")
        <= value(base_card, "negatives_fp"),
        "hallucinated_zero": head_card["hallucinated_aliases"] == 0,
        "no_family_loses_more_than_one": all(loss <= 1 for loss in family_loss.values()),
    }
    return {
        "target": target or "family_accuracy",
        "hard_paired_difference": hard_diff,
        "family_loss": family_loss,
        **rule,
        "keep": all(rule.values()),
    }
