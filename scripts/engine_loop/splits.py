"""Dataset v2 identities and split assignment.

- Case ids are opaque: ``c-`` plus 12 hex characters of ``sha256(seed)``. Scenario names
  live only in the meta and label files.
- The unit of assignment is a trial, or a contrast pair when the trial has a
  ``contrast_group``, so both members of a pair always share a split.
- Trials with a ``holdout`` go to test (the out-of-distribution stratum).
- Within each scenario cell, units are ordered by ``sha256(unit)`` and alternate between
  test and dev, the first to test.
- Dev-check is a fixed share of dev trials (20%, at least 30 when dev is large enough),
  spread over the cells: each dev unit gets the rank ``(j + 0.5) / n`` of its position in
  its cell, and units are taken in rank order until the share is reached.

Assignment depends only on the seeds and cells, never on the collecting host.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

SPLIT_VERSION = "EngineSplit/v2"
DEV_CHECK_SHARE = 0.2
DEV_CHECK_MIN = 30


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def opaque_id(seed: str) -> str:
    return "c-" + digest(seed)[:12]


def _units(trials: list[dict]) -> dict[str, list[dict]]:
    units: dict[str, list[dict]] = defaultdict(list)
    for trial in trials:
        units[trial.get("contrast_group") or trial["seed"]].append(trial)
    return units


def assign(trials: list[dict]) -> dict[str, dict]:
    """Return ``{seed: {"split": "dev"|"test", "dev_check": bool}}``.

    Each trial needs ``seed`` and ``cell``; ``contrast_group`` and ``holdout`` are optional.
    """
    seeds = [trial["seed"] for trial in trials]
    if len(set(seeds)) != len(seeds):
        raise ValueError("trial seeds must be unique")
    result: dict[str, dict] = {}
    cells: dict[str, list[tuple[str, list[dict]]]] = defaultdict(list)
    for unit, members in _units(trials).items():
        held = {member.get("holdout") or "none" for member in members}
        if held != {"none"}:
            if "none" in held:
                raise ValueError(f"contrast group {unit} mixes held-out and in-distribution trials")
            for member in members:
                result[member["seed"]] = {"split": "test", "dev_check": False}
            continue
        cell = "|".join(sorted({member["cell"] for member in members}))
        cells[cell].append((unit, members))
    ranked: list[tuple[float, str, list[dict]]] = []
    for cell in sorted(cells):
        ordered = sorted(cells[cell], key=lambda item: digest(item[0]))
        dev_units = []
        for index, (unit, members) in enumerate(ordered):
            split = "test" if index % 2 == 0 else "dev"
            for member in members:
                result[member["seed"]] = {"split": split, "dev_check": False}
            if split == "dev":
                dev_units.append((unit, members))
        for position, (unit, members) in enumerate(dev_units):
            ranked.append(((position + 0.5) / len(dev_units), digest(unit), members))
    dev_trials = sum(len(members) for _, _, members in ranked)
    target = max(math.ceil(DEV_CHECK_SHARE * dev_trials), min(DEV_CHECK_MIN, dev_trials // 2))
    chosen = 0
    for _, _, members in sorted(ranked, key=lambda item: (item[0], item[1])):
        if chosen >= target:
            break
        for member in members:
            result[member["seed"]]["dev_check"] = True
        chosen += len(members)
    return result
