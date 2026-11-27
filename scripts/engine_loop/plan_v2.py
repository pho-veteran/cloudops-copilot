"""Dataset v2 generator plan, observability check and herring presence check.

The plan is the pre-registered cell table in docs/engine-loop/dataset-v2.md. Every trial
has a seed (``<prefix>/<cell>/<index>``), an opaque case id and a split decided here,
before collection, by ``splits.assign`` over the whole plan. The host is recorded but
never used for assignment: within each cell even indexes run on the laptop and odd
indexes on EC2, so each host covers every cell.

The observability check reads only the collected bundle. It confirms the decisive cue of
the scenario; a positive whose cue is missing is relabelled ``unknown`` and tagged
``silent``. A healed herring whose earlier failure is not visible is tagged
``herring_absent``. Both results are recorded in the case meta.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from engine_loop import bundle as raw
from engine_loop import splits

PLAN_VERSION = "EngineLabPlan/v2"
HOSTS = ("laptop", "ec2")

# cell -> (group, kind for labelling, mechanism steps, trials). A step is
# (mechanism, params). ``kind`` is the v1 fault kind whose family labels the cell when its
# cue is observed; ``None`` labels the cell ``unknown``.
_C = "crashloop"
CELLS: dict[str, dict] = {
    # crashloop_startup
    "crash-startup": {"kind": _C, "steps": [("crash-startup", {})], "n": 10},
    "crash-after": {"kind": _C, "steps": [("crash-after", {"after_s": "20"})], "n": 10},
    "crash-setting": {"kind": _C, "steps": [("crash-setting", {})], "n": 10},
    "liveness-clean": {"kind": _C, "steps": [("liveness", {})], "n": 6},
    "liveness-137": {"kind": _C, "steps": [("liveness", {"ignore_term": "1"})], "n": 6},
    # oom_restarts
    "oom-strong": {
        "kind": "oom",
        "steps": [("oom-alloc", {"limit": "32Mi", "alloc_mib": "128"})],
        "n": 12,
        "intensity": "strong",
    },
    "oom-near": {
        "kind": "oom",
        "steps": [("oom-alloc", {"limit": "96Mi", "alloc_mib": "96"})],
        "n": 12,
        "intensity": "near",
    },
    "oom-leak": {"kind": "oom", "steps": [("oom-leak", {"limit": "96Mi"})], "n": 10},
    # traffic_latency_errors
    "slow-strong": {
        "kind": "traffic",
        "steps": [("slow", {"delay_ms": "400"})],
        "n": 6,
        "intensity": "strong",
    },
    "slow-near": {
        "kind": "traffic",
        "steps": [("slow", {"delay_ms": "400", "errors": "0", "share": "0.06"})],
        "n": 6,
        "intensity": "near",
    },
    "errors-strong": {
        "kind": "traffic",
        "steps": [("errors", {"fraction": "0.5"})],
        "n": 6,
        "intensity": "strong",
    },
    "errors-near": {
        "kind": "traffic",
        "steps": [("errors", {"fraction": "0.08"})],
        "n": 6,
        "intensity": "near",
    },
    "throttle": {
        "kind": "traffic",
        "steps": [("throttle", {"cpu": "25m", "loops": "100000"})],
        "n": 8,
    },
    "overload": {"kind": "traffic", "steps": [], "load": 25, "n": 8},
    # pending_unschedulable
    "pending-cpu": {"kind": "pending", "steps": [("pending-cpu", {})], "n": 12},
    "pending-memory": {"kind": "pending", "steps": [("pending-memory", {})], "n": 12},
    "pending-selector": {"kind": "pending", "steps": [("pending-selector", {})], "n": 10},
    # image_pull
    "image-unreachable": {"kind": "imagepull", "steps": [("image-unreachable", {})], "n": 12},
    "image-invalid": {"kind": "imagepull", "steps": [("image-invalid", {})], "n": 12},
    "image-never": {"kind": "imagepull", "steps": [("image-never", {})], "n": 10},
    # look-alikes (unknown)
    "config-ref": {"kind": None, "lookalike": "crashloop", "steps": [("config-ref", {})], "n": 10},
    "memory-near": {
        "kind": None,
        "lookalike": "oom",
        "steps": [("memory-hold", {"limit": "96Mi", "hold_mib": "64"})],
        "n": 12,
    },
    "quota": {"kind": None, "lookalike": "pending", "steps": [("quota", {})], "n": 12},
    "readiness-wrong": {
        "kind": None,
        "lookalike": "traffic",
        "steps": [("readiness-wrong", {})],
        "n": 6,
    },
    "load-ramp": {"kind": None, "lookalike": "traffic", "steps": [], "load": 10, "n": 8},
    "scale-zero": {"kind": None, "lookalike": "any", "steps": [("scale-zero", {})], "n": 6},
    "benign-rollout": {
        "kind": None,
        "lookalike": "rollout",
        "steps": [("benign", {"ready_delay_s": "20"})],
        "n": 8,
    },
    # herrings and healthy (unknown)
    "healthy": {"kind": None, "steps": [], "n": 14},
    "healed-oom": {"kind": None, "herring": "oom", "pre": [("once", {"kind": "oom"})], "n": 8},
    "healed-crash": {
        "kind": None,
        "herring": "crashloop",
        "pre": [("once", {"kind": "crash"})],
        "n": 8,
    },
    "healthy-untrusted": {"kind": None, "steps": [], "untrusted": True, "n": 8},
    # collected missing sources
    "missing-prometheus": {
        "kind": "traffic",
        "steps": [("errors", {"fraction": "0.5"})],
        "missing": "prometheus",
        "n": 6,
    },
    "missing-backoff": {
        "kind": _C,
        "steps": [("crash-startup", {})],
        "missing": "backoff",
        "n": 6,
    },
    "missing-events-crash": {
        "kind": _C,
        "steps": [("crash-startup", {})],
        "missing": "events",
        "n": 3,
    },
    "missing-events-pending": {
        "kind": "pending",
        "steps": [("pending-cpu", {})],
        "missing": "events",
        "n": 3,
    },
}
# Held out for the out-of-distribution test stratum: log profile ``kv`` on one cell of each
# family and healthy (4 each), and the ``mid`` intensity of the OOM and error ladders.
HOLDOUT_CELLS: dict[str, dict] = {
    **{
        f"kv-{cell}": {**CELLS[cell], "n": 4, "holdout": "kv", "base": cell}
        for cell in (
            "crash-startup",
            "oom-strong",
            "errors-strong",
            "pending-cpu",
            "image-unreachable",
            "healthy",
        )
    },
    "oom-mid": {
        "kind": "oom",
        "steps": [("oom-alloc", {"limit": "64Mi", "alloc_mib": "96"})],
        "n": 4,
        "intensity": "mid",
        "holdout": "mid",
    },
    "errors-mid": {
        "kind": "traffic",
        "steps": [("errors", {"fraction": "0.2"})],
        "n": 4,
        "intensity": "mid",
        "holdout": "mid",
    },
}
# Contrast pairs: member i of the first cell pairs with member i of the second.
PAIRS = (
    ("liveness-137", "oom-strong"),
    ("config-ref", "crash-startup"),
    ("memory-near", "oom-near"),
    ("quota", "pending-cpu"),
    ("readiness-wrong", "slow-strong"),
    ("load-ramp", "overload"),
)
FAMILY_BY_KIND = {
    "crashloop": "crashloop_startup",
    "oom": "oom_restarts",
    "traffic": "traffic_latency_errors",
    "pending": "pending_unschedulable",
    "imagepull": "image_pull",
}
# Plausible wrong diagnoses, stated as claims, never as commands to the reader.
CLAIMS = {
    "crashloop_startup": "checkout keeps crash looping since the last deploy; the new build is bad",
    "oom_restarts": "pods were OOMKilled overnight; the memory limit is too low",
    "traffic_latency_errors": "latency spiked to several seconds and users see 503 errors",
    "pending_unschedulable": "new pods stay Pending; the cluster is out of CPU",
    "image_pull": "the registry rejected our credentials, so the image pull is failing",
}
CLAIM_ORDER = tuple(CLAIMS)


def _hash(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def _claim_for(gold: str, seed: str) -> str:
    choices = [family for family in CLAIM_ORDER if family != gold]
    return choices[_hash(seed + "/claim") % len(choices)]


def build(prefix: str = "lab-v4") -> list[dict]:
    """The full plan, in an interleaved order, with recorded split and host."""
    cells = {**CELLS, **HOLDOUT_CELLS}
    pair_of: dict[tuple[str, int], str] = {}
    for left, right in PAIRS:
        for index in range(min(cells[left]["n"], cells[right]["n"])):
            group = f"{prefix}/pair/{left}+{right}/{index}"
            pair_of[(left, index)] = group
            pair_of[(right, index)] = group
    trials = []
    for cell, spec in cells.items():
        for index in range(spec["n"]):
            seed = f"{prefix}/{cell}/{index}"
            family = FAMILY_BY_KIND.get(spec["kind"]) if spec["kind"] else "unknown"
            holdout = spec.get("holdout", "none")
            profile = "kv" if holdout == "kv" else ("json" if _hash(seed) % 2 else "plain")
            untrusted = None
            if spec.get("untrusted"):
                untrusted = _claim_for("unknown", seed)
            elif family != "unknown" and holdout == "none" and _hash(seed + "/u") % 4 == 0:
                untrusted = _claim_for(family, seed)
            steps = list(spec.get("steps", []))
            if cell == "healthy" and index % 2 == 1:
                # Half the healthy trials roll out a harmless change, so a rollout alone
                # never marks a fault.
                steps = [("benign", {})]
            trials.append(
                {
                    "seed": seed,
                    "case_id": splits.opaque_id(seed),
                    "cell": cell,
                    "index": index,
                    "scenario": spec.get("base", cell),
                    "kind": spec["kind"],
                    "family": family,
                    "mechanism": cell,
                    "intensity": spec.get("intensity"),
                    "onset": "late" if _hash(seed + "/onset") % 3 == 0 else "early",
                    "steps": [[name, dict(params)] for name, params in steps],
                    "pre": [[name, dict(params)] for name, params in spec.get("pre", [])],
                    "load": spec.get("load"),
                    "missing": spec.get("missing"),
                    "lookalike": spec.get("lookalike"),
                    "herring": spec.get("herring"),
                    "holdout": holdout,
                    "log_profile": profile,
                    "untrusted": (
                        {"claims": untrusted, "text": CLAIMS[untrusted]} if untrusted else None
                    ),
                    "contrast_group": pair_of.get((cell, index)),
                    "host": HOSTS[index % 2],
                }
            )
    assignment = splits.assign(
        [
            {
                "seed": trial["seed"],
                "cell": trial["cell"],
                "contrast_group": trial["contrast_group"],
                "holdout": trial["holdout"],
            }
            for trial in trials
        ]
    )
    for trial in trials:
        trial.update(assignment[trial["seed"]])
    # Round-robin over cells by index, so a partial batch still covers every cell.
    return sorted(trials, key=lambda item: (item["index"], _hash(item["cell"])))


# --- observability -------------------------------------------------------------------


def _slots(value) -> dict:
    return {item.slot: item for item in value.slots}


def _pods(value) -> list[dict]:
    state = _slots(value).get("kubernetes.state")
    return ((state.payload or {}).get("pods") or []) if state else []


def _events(value) -> list[dict]:
    events = _slots(value).get("kubernetes.events")
    return ((events.payload or {}).get("events") or []) if events else []


def _containers(value):
    for pod in _pods(value):
        for container in pod.get("containers") or []:
            yield pod, container


def _stamp(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _series_values(value, slot: str) -> list[float]:
    item = _slots(value).get(slot)
    if item is None or item.payload is None:
        return []
    return [
        sample[1]
        for series in item.payload.get("series") or []
        for sample in series.get("samples") or []
        if len(sample) > 1 and isinstance(sample[1], (int, float))
    ]


def cues(value) -> dict:
    """Decisive cues present in a bundle, with the window-relative restart facts."""
    window = value.scope.window
    reasons = {event.get("reason") for event in _events(value)}
    waiting = set()
    terminated_in_window: list[str] = []
    terminated_before: list[str] = []
    restarts = 0
    for _, container in _containers(value):
        restarts += container.get("restart_count") or 0
        detail = (container.get("state") or {}).get("waiting") or {}
        if detail.get("reason"):
            waiting.add(detail["reason"])
        last = (container.get("last_state") or {}).get("terminated") or {}
        finished = _stamp(last.get("finished_at"))
        if last.get("reason") and finished:
            target = terminated_in_window if finished >= window.start else terminated_before
            target.append(last["reason"])
    errors = _series_values(value, "prometheus.error_ratio")
    p95 = _series_values(value, "prometheus.p95_latency")
    return {
        "event_reasons": sorted(reason for reason in reasons if reason),
        "waiting": sorted(waiting),
        "restarts": restarts,
        "terminated_in_window": sorted(terminated_in_window),
        "terminated_before_window": sorted(terminated_before),
        "error_samples_high": sum(1 for item in errors if item >= 0.05),
        "p95_samples_high": sum(1 for item in p95 if item >= 0.25),
    }


IMAGE_WAITING = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}


def observe(value, trial: dict) -> dict:
    """The observability check for one collected trial."""
    found = cues(value)
    kind = trial["kind"]
    cell = trial["cell"]
    if kind == "crashloop":
        if cell.startswith("liveness") or trial.get("scenario", "").startswith("liveness"):
            ok = {"Unhealthy", "Killing"} <= set(found["event_reasons"]) and found["restarts"] > 0
            cue = "liveness Unhealthy and Killing events with a restart"
        elif trial.get("missing") == "events":
            ok = found["restarts"] > 0
            cue = "restart in state (events purged)"
        else:
            ok = found["restarts"] > 0 and any(
                reason != "OOMKilled" for reason in found["terminated_in_window"]
            )
            cue = "non-OOM termination inside the window"
    elif kind == "oom":
        ok = "OOMKilled" in found["terminated_in_window"]
        cue = "OOMKilled termination inside the window"
    elif kind == "traffic" and trial.get("missing") == "prometheus":
        ok = not any(
            item.coverage_status in {"available", "partial"}
            for item in value.slots
            if item.slot.startswith("prometheus.")
        )
        cue = "every Prometheus slot unavailable (scrape source stopped)"
    elif kind == "traffic":
        ok = found["error_samples_high"] >= 2 or found["p95_samples_high"] >= 2
        cue = "error ratio >= 0.05 or p95 >= 0.25 s for at least two samples"
    elif kind == "pending":
        ok = "FailedScheduling" in found["event_reasons"] or (
            trial.get("missing") == "events"
            and any(pod.get("status") == "Pending" for pod in _pods(value))
        )
        cue = "FailedScheduling event (or a Pending Pod when events were purged)"
    elif kind == "imagepull":
        ok = bool(IMAGE_WAITING & set(found["waiting"]))
        cue = "image pull waiting reason"
    else:
        lookalike_cues = {
            "config-ref": ("CreateContainerConfigError" in found["waiting"], "config error"),
            "quota": ("FailedCreate" in found["event_reasons"], "ReplicaSet FailedCreate event"),
            "memory-near": (
                "OOMKilled" not in found["terminated_in_window"],
                "no OOM kill inside the window",
            ),
            "readiness-wrong": ("Unhealthy" in found["event_reasons"], "readiness Unhealthy"),
            "scale-zero": (not _pods(value), "no Pod"),
        }
        ok, cue = lookalike_cues.get(trial["scenario"], (True, "none required"))
    result = {"cue": cue, "present": bool(ok), "cues": found}
    if trial.get("herring"):
        reason = "OOMKilled" if trial["herring"] == "oom" else None
        before = found["terminated_before_window"]
        present = bool(before) and (reason is None or reason in before)
        result["herring_present"] = present and not found["terminated_in_window"]
    return result


def expected_kind(trial: dict, observation: dict) -> tuple[str | None, list[str]]:
    """The fault kind that labels the case, and its tags."""
    tags = []
    kind = trial["kind"]
    if kind and not observation["present"]:
        tags.append("silent")
        kind = None
    if trial.get("lookalike") and not observation["present"]:
        tags.append("lookalike_cue_absent")
    if trial.get("herring") and not observation.get("herring_present"):
        tags.append("herring_absent")
    return kind, tags


def distractors(value, trial: dict) -> list[dict]:
    """Entries that belong to a red herring or carry untrusted text on an abstention."""
    found = []
    window = value.scope.window
    if trial.get("herring"):
        for material in raw.materials(value):
            if material.slot == "kubernetes.state":
                for entry in material.entries:
                    if "last_state" in entry.identity or "restart_count" in entry.identity:
                        found.append({"slot": material.slot, "key": entry.key})
            elif material.slot == "kubernetes.events":
                for entry in material.entries:
                    seen = _stamp((entry.value or {}).get("last_seen"))
                    if entry.value.get("type") == "Warning" and seen and seen < window.start:
                        found.append({"slot": material.slot, "key": entry.key})
    note = (trial.get("untrusted") or {}).get("text")
    if note and trial["family"] == "unknown":
        for material in raw.materials(value):
            if material.slot.startswith("kubernetes.logs."):
                for entry in material.entries:
                    if note in str((entry.value or {}).get("text", "")):
                        found.append({"slot": material.slot, "key": entry.key})
    return found
