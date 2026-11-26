"""Stage A: deterministic signal extraction from projected slot entries.

Input is the unselected `SlotMaterial` entries (stored, redacted evidence). Output is a
`SignalTable` of typed facts. Every signal keeps references to the exact entries that
support it, so a later citation of that signal resolves to sent aliases only.
Untrusted text (log lines, event messages) only ever feeds closed keyword classes; it
never becomes an instruction or a free-form signal name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from cloudops.features.diagnosis.context import SlotMaterial
from cloudops.features.investigations.locators import Entry, sample_number, timestamp

SIGNALS_VERSION = "engine.signals/v1"
LOG_SLOTS = ("kubernetes.logs.current", "kubernetes.logs.previous")


@dataclass(frozen=True, order=True)
class EntryRef:
    slot: str
    evidence_id: str
    kind: str
    key: str


@dataclass(frozen=True)
class Signal:
    name: str
    slot: str
    value: object
    support: tuple[EntryRef, ...]


@dataclass
class SignalTable:
    signals: list[Signal] = field(default_factory=list)

    def add(self, name: str, slot: str, value, support) -> None:
        refs = tuple(sorted(set(support)))
        if refs:
            self.signals.append(Signal(name, slot, value, refs))

    def named(self, name: str) -> list[Signal]:
        return [item for item in self.signals if item.name == name]

    def has(self, name: str) -> bool:
        return any(item.name == name for item in self.signals)

    def value(self, name: str, default=None):
        found = self.named(name)
        return found[0].value if found else default

    def records(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "slot": item.slot,
                "value": item.value,
                "support": len(item.support),
            }
            for item in self.signals
        ]


def _ref(material: SlotMaterial, entry: Entry) -> EntryRef:
    return EntryRef(material.slot, str(material.evidence_id), entry.kind, entry.key)


def _path(entry: Entry) -> list[str]:
    try:
        value = json.loads(entry.identity)
    except (TypeError, ValueError):
        return []
    return [str(part) for part in value] if isinstance(value, list) else []


def _number(value) -> Decimal | None:
    return sample_number(value)


# --- state ---------------------------------------------------------------------------

WAITING_CRASH = {"crashloopbackoff"}
WAITING_IMAGE = {"imagepullbackoff", "errimagepull", "invalidimagename", "errimageneverpull"}


def _state(material: SlotMaterial, table: SignalTable) -> None:
    """Live state (pods/containers/deployment) and the flat E06 fixture shape."""
    containers: dict[tuple, dict[str, tuple[object, EntryRef]]] = {}
    flat: dict[str, list[tuple[object, EntryRef]]] = {}
    pending: list[EntryRef] = []
    unschedulable: list[EntryRef] = []
    unavailable: list[EntryRef] = []
    memory_limits: list[tuple[str, EntryRef]] = []
    for entry in material.entries:
        if entry.kind != "field":
            continue
        path = _path(entry)
        if not path:
            continue
        ref = _ref(material, entry)
        value = entry.value
        text = value.lower() if isinstance(value, str) else None
        if path[0] == "pods" and "containers" in path:
            index = path.index("containers")
            key = tuple(path[: index + 2])
            containers.setdefault(key, {})["/".join(path[index + 2 :])] = (value, ref)
            continue
        leaf = path[-1]
        if path[0] == "pods":
            if leaf in {"status", "phase"} and len(path) == 3 and text == "pending":
                pending.append(ref)
            elif leaf == "reason" and text == "unschedulable":
                unschedulable.append(ref)
            elif leaf in {
                "waiting_reason",
                "last_termination_reason",
                "exit_code",
                "restart_count",
            }:
                flat.setdefault(leaf, []).append((value, ref))
            elif leaf == "scheduled" and value is False:
                unschedulable.append(ref)
        elif path[0] == "deployment":
            if leaf == "memory" and "limits" in path and isinstance(value, str):
                memory_limits.append((value, ref))
            elif leaf == "status" and text == "false" and any('"Available"' in p for p in path):
                unavailable.append(ref)
    terminated: dict[str, list[EntryRef]] = {}
    exit_codes: dict[int, list[EntryRef]] = {}
    waiting: dict[str, list[EntryRef]] = {}
    restarts: list[tuple[int, EntryRef]] = []
    init_failing: list[EntryRef] = []
    for fields in containers.values():
        kind = fields.get("kind", (None, None))[0]
        refs_here = []
        for name in ("last_state/terminated/reason", "state/terminated/reason"):
            if name in fields and isinstance(fields[name][0], str):
                terminated.setdefault(fields[name][0], []).append(fields[name][1])
                refs_here.append(fields[name][1])
        for name in ("last_state/terminated/exit_code", "state/terminated/exit_code"):
            if name in fields and isinstance(fields[name][0], int):
                exit_codes.setdefault(fields[name][0], []).append(fields[name][1])
        if "state/waiting/reason" in fields and isinstance(fields["state/waiting/reason"][0], str):
            waiting.setdefault(fields["state/waiting/reason"][0], []).append(
                fields["state/waiting/reason"][1]
            )
            refs_here.append(fields["state/waiting/reason"][1])
        count = fields.get("restart_count", (None, None))
        if isinstance(count[0], int) and not isinstance(count[0], bool):
            restarts.append((count[0], count[1]))
            if count[0] > 0:
                refs_here.append(count[1])
        if kind == "init" and refs_here:
            init_failing.extend([fields["kind"][1], *refs_here])
    for value, ref in flat.get("last_termination_reason", []):
        if isinstance(value, str):
            terminated.setdefault(value, []).append(ref)
    for value, ref in flat.get("exit_code", []):
        if isinstance(value, int) and not isinstance(value, bool):
            exit_codes.setdefault(value, []).append(ref)
    for value, ref in flat.get("waiting_reason", []):
        if isinstance(value, str):
            waiting.setdefault(value, []).append(ref)
    for value, ref in flat.get("restart_count", []):
        if isinstance(value, int) and not isinstance(value, bool):
            restarts.append((value, ref))
    for reason, refs in terminated.items():
        table.add("termination_reason", material.slot, reason, refs)
    for code, refs in exit_codes.items():
        table.add("exit_code", material.slot, code, refs)
    for reason, refs in waiting.items():
        table.add("waiting_reason", material.slot, reason, refs)
    positive = [(count, ref) for count, ref in restarts if count > 0]
    if positive:
        table.add(
            "restart_count",
            material.slot,
            max(count for count, _ in positive),
            [ref for _, ref in positive],
        )
    table.add("pod_pending", material.slot, len(pending), pending)
    table.add("unschedulable", material.slot, True, unschedulable)
    table.add("init_container_failing", material.slot, True, init_failing)
    table.add("deployment_unavailable", material.slot, True, unavailable)
    for limit, ref in memory_limits:
        table.add("memory_limit", material.slot, limit, [ref])


# --- events ----------------------------------------------------------------------------

EVENT_CLASSES = (
    ("event_oom", re.compile(r"oom|out of memory|memory limit|exceeded its .*limit")),
    ("event_backoff_restart", re.compile(r"back-off restarting|crashloop")),
    (
        "event_image_pull",
        re.compile(
            r"pull(ing)? image|errimagepull|imagepullbackoff|manifest unknown|not found|unauthorized|pull access denied"
        ),
    ),
    (
        "event_insufficient",
        re.compile(
            r"insufficient (cpu|memory)|untolerated taint|didn't match|nodes are available|unschedulable"
        ),
    ),
    ("event_probe_failed", re.compile(r"probe failed|readiness probe|liveness probe")),
)
REASON_CLASSES = {
    "failedscheduling": "event_failed_scheduling",
    "oomkilling": "event_oom",
    "backoff": "event_backoff",
    "failed": "event_failed",
    "unhealthy": "event_probe_failed",
    "errimagepull": "event_image_pull",
    "imagepullbackoff": "event_image_pull",
}


def _events(material: SlotMaterial, table: SignalTable) -> None:
    groups: dict[str, list[tuple[Entry, int]]] = {}
    classes: dict[str, list[EntryRef]] = {}
    for entry in material.entries:
        if entry.kind != "event" or not isinstance(entry.value, dict):
            continue
        value = entry.value
        reason = str(value.get("reason") or "")
        message = str(value.get("message") or "").lower()
        count = value.get("count") if isinstance(value.get("count"), int) else 1
        groups.setdefault(reason, []).append((entry, count))
        ref = _ref(material, entry)
        mapped = REASON_CLASSES.get(reason.lower())
        if mapped:
            classes.setdefault(mapped, []).append(ref)
        for name, pattern in EVENT_CLASSES:
            if pattern.search(message):
                classes.setdefault(name, []).append(ref)
        if str(value.get("type") or "").lower() == "warning":
            classes.setdefault("event_warning", []).append(ref)
    for reason, items in sorted(groups.items()):
        total = sum(count for _, count in items)
        table.add(
            "event_group",
            material.slot,
            {"reason": reason, "objects": len(items), "count": total},
            [_ref(material, entry) for entry, _ in items],
        )
    for name, refs in sorted(classes.items()):
        table.add(name, material.slot, len(refs), refs)


# --- logs ----------------------------------------------------------------------------

LOG_CLASSES = (
    ("log_oom", re.compile(r"out of memory|memoryerror|cannot allocate|oom|killed")),
    (
        "log_startup_failure",
        re.compile(
            r"rejected|fatal|missing|exiting status|address already in use|bind|"
            r"traceback|exception|panic|startup failed|configuration error"
        ),
    ),
    ("log_error", re.compile(r"\berror\b|\bfailed\b|\bfailure\b")),
)
_MASK = re.compile(r"\b\d{4}-\d{2}-\d{2}t[\d:.]+z?\b|0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\d+")


def _lines(value) -> list[str]:
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("lines"), list):
        return [str(line) for line in value["lines"]]
    if isinstance(value.get("text"), str):
        return value["text"].splitlines()
    return []


def _template(line: str) -> str:
    text = line.strip().lower()
    parts = text.split(" ", 1)
    if parts and timestamp(parts[0].upper()) is not None and len(parts) == 2:
        text = parts[1]
    return _MASK.sub("<n>", text)[:160]


def _logs(material: SlotMaterial, table: SignalTable) -> None:
    classes: dict[str, list[EntryRef]] = {}
    templates: dict[str, list[EntryRef]] = {}
    lines_total = 0
    for entry in material.entries:
        if entry.kind != "log_segment":
            continue
        ref = _ref(material, entry)
        lines = _lines(entry.value)
        lines_total += len(lines)
        for line in lines:
            template = _template(line)
            templates.setdefault(template, []).append(ref)
            for name, pattern in LOG_CLASSES:
                if pattern.search(template):
                    classes.setdefault(name, []).append(ref)
                    break
    table.add(
        "log_lines",
        material.slot,
        lines_total,
        {ref for refs in templates.values() for ref in refs},
    )
    table.add(
        "log_templates",
        material.slot,
        len(templates),
        {ref for refs in templates.values() for ref in refs},
    )
    for name, refs in sorted(classes.items()):
        table.add(name, material.slot, len(refs), refs)


# --- series --------------------------------------------------------------------------


def _samples(material: SlotMaterial) -> list[tuple[Decimal, Decimal, EntryRef]]:
    values = []
    for entry in material.entries:
        if entry.kind != "sample":
            continue
        path = _path(entry) if entry.identity.startswith("[") else []
        when = timestamp(path[-1]) if path else None
        number = _number(entry.value)
        if when is None or number is None:
            continue
        values.append((Decimal(when), number, _ref(material, entry)))
    return sorted(values, key=lambda item: item[0])


def series_features(material: SlotMaterial) -> dict | None:
    samples = _samples(material)
    if not samples:
        return None
    values = [value for _, value, _ in samples]
    third = max(1, len(values) // 3)
    baseline = values[:third]
    recent = values[-third:]
    base_mean = sum(baseline) / len(baseline)
    recent_mean = sum(recent) / len(recent)
    peak_index = max(range(len(values)), key=lambda index: values[index])
    return {
        "n": len(values),
        "first": float(values[0]),
        "last": float(values[-1]),
        "max": float(values[peak_index]),
        "mean": float(sum(values) / len(values)),
        "baseline_mean": float(base_mean),
        "recent_mean": float(recent_mean),
        "ratio": float(recent_mean / base_mean) if base_mean > 0 else None,
        "support": [samples[peak_index][2], samples[-1][2], samples[0][2]],
    }


def _series(material: SlotMaterial, table: SignalTable, features: dict) -> None:
    value = series_features(material)
    if value is None:
        return
    support = value.pop("support")
    features[material.slot] = value
    name = material.slot.removeprefix("prometheus.")
    table.add(f"metric_{name}", material.slot, value, support)


def extract(materials: list[SlotMaterial]) -> SignalTable:
    table = SignalTable()
    features: dict[str, dict] = {}
    for material in materials:
        if material.content_status != "available" or material.slot == "development_fixture":
            continue
        if material.slot == "kubernetes.state":
            _state(material, table)
        elif material.slot == "kubernetes.events":
            _events(material, table)
        elif material.slot in LOG_SLOTS:
            _logs(material, table)
        elif material.slot.startswith("prometheus."):
            _series(material, table, features)
    working = features.get("prometheus.memory_working_set")
    limit = features.get("prometheus.memory_limit")
    if working and limit and limit["max"] > 0:
        share = working["max"] / limit["max"]
        refs = [
            ref
            for signal in table.signals
            if signal.name in {"metric_memory_working_set", "metric_memory_limit"}
            for ref in signal.support[:1]
        ]
        table.add("memory_share_of_limit", "prometheus.memory_working_set", round(share, 4), refs)
    restarts = features.get("prometheus.restarts")
    if restarts:
        delta = restarts["last"] - restarts["first"]
        refs = [ref for signal in table.named("metric_restarts") for ref in signal.support]
        table.add("restart_delta", "prometheus.restarts", delta, refs)
    return table
