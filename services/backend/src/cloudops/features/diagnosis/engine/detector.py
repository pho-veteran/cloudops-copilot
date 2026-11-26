"""Stage B: explainable family detector over a stage A signal table.

Each family score is a sum of fixed, named feature weights (versioned below). The
detector abstains when the top score is under the threshold, when the top two scores are
within the margin, or when the top family is not eligible in the product family matrix.
Every feature keeps the entry references that support it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cloudops.features.diagnosis.context import FamilyAssessment
from cloudops.features.diagnosis.engine.signals import EntryRef, SignalTable

DETECTOR_VERSION = "engine.detector/v1"
THRESHOLD = 3.0
MARGIN = 1.0
FAMILIES = (
    "crashloop_startup",
    "oom_restarts",
    "traffic_latency_errors",
    "pending_unschedulable",
    "image_pull",
)
# family -> feature -> weight. Negative weights are explanations that compete.
WEIGHTS: dict[str, dict[str, float]] = {
    "oom_restarts": {
        "termination_oomkilled": 4.0,
        "exit_code_137": 2.0,
        "event_oom": 2.0,
        "memory_near_limit": 1.0,
        "log_oom": 0.5,
        "restarts": 0.5,
    },
    "crashloop_startup": {
        "termination_error": 2.0,
        "waiting_crashloop": 2.0,
        "event_backoff_restart": 2.0,
        "exit_code_nonzero": 1.0,
        "log_startup_failure": 1.5,
        "init_container_failing": 1.0,
        "restarts": 1.0,
        "termination_oomkilled": -3.0,
    },
    "traffic_latency_errors": {
        "error_ratio_high": 2.0,
        "p95_high": 2.0,
        "p95_increase": 1.0,
        "error_increase": 1.0,
    },
    "pending_unschedulable": {
        "unschedulable": 3.0,
        "event_failed_scheduling": 2.0,
        "event_insufficient": 1.0,
        "pod_pending": 0.5,
    },
    "image_pull": {
        "waiting_image_pull": 3.0,
        "event_image_pull": 2.0,
    },
}
ERROR_RATIO_HIGH = 0.05
P95_HIGH_SECONDS = 0.25
INCREASE_RATIO = 2.0
MEMORY_NEAR_LIMIT = 0.9
IMAGE_WAITING = {"imagepullbackoff", "errimagepull", "invalidimagename", "errimageneverpull"}


@dataclass(frozen=True)
class Feature:
    name: str
    value: object
    support: tuple[EntryRef, ...]


@dataclass
class Hypothesis:
    family: str
    score: float
    features: list[tuple[str, float]]
    support: tuple[EntryRef, ...]
    eligible: bool
    missing_required_slots: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {
            "family": self.family,
            "score": round(self.score, 3),
            "features": [[name, weight] for name, weight in self.features],
            "eligible": self.eligible,
            "missing_required_slots": self.missing_required_slots,
        }


@dataclass
class Detection:
    family: str  # a family name, or "unknown" when the detector abstains
    reason: str
    hypotheses: list[Hypothesis]

    def record(self) -> dict:
        return {
            "version": DETECTOR_VERSION,
            "family": self.family,
            "reason": self.reason,
            "hypotheses": [item.record() for item in self.hypotheses],
        }


def features(table: SignalTable) -> dict[str, Feature]:
    found: dict[str, Feature] = {}

    def put(name, value, support):
        support = tuple(sorted(set(support)))
        if not support:
            return
        if name in found:
            support = tuple(sorted(set(found[name].support) | set(support)))
        found[name] = Feature(name, value, support)

    for signal in table.named("termination_reason"):
        reason = str(signal.value).lower()
        if reason == "oomkilled":
            put("termination_oomkilled", True, signal.support)
        elif reason in {"error", "startuperror", "containercannotrun"}:
            put("termination_error", True, signal.support)
    for signal in table.named("exit_code"):
        if signal.value == 137:
            put("exit_code_137", True, signal.support)
        elif signal.value not in (0, 137):
            put("exit_code_nonzero", signal.value, signal.support)
    for signal in table.named("waiting_reason"):
        reason = str(signal.value).lower()
        if reason == "crashloopbackoff":
            put("waiting_crashloop", True, signal.support)
        elif reason in IMAGE_WAITING:
            put("waiting_image_pull", True, signal.support)
    for name in ("restart_count",):
        for signal in table.named(name):
            put("restarts", signal.value, signal.support)
    for signal in table.named("restart_delta"):
        if isinstance(signal.value, (int, float)) and signal.value > 0:
            put("restarts", signal.value, signal.support)
    for name, feature in (
        ("event_oom", "event_oom"),
        ("event_backoff_restart", "event_backoff_restart"),
        ("event_failed_scheduling", "event_failed_scheduling"),
        ("event_insufficient", "event_insufficient"),
        ("event_image_pull", "event_image_pull"),
        ("log_oom", "log_oom"),
        ("log_startup_failure", "log_startup_failure"),
        ("unschedulable", "unschedulable"),
        ("init_container_failing", "init_container_failing"),
    ):
        for signal in table.named(name):
            put(feature, signal.value, signal.support)
    for signal in table.named("pod_pending"):
        put("pod_pending", signal.value, signal.support)
    for signal in table.named("memory_share_of_limit"):
        if signal.value >= MEMORY_NEAR_LIMIT:
            put("memory_near_limit", signal.value, signal.support)
    for signal in table.named("metric_error_ratio"):
        value = signal.value
        if value["max"] >= ERROR_RATIO_HIGH:
            put("error_ratio_high", value["max"], signal.support)
        if value["ratio"] is not None and value["ratio"] >= INCREASE_RATIO and value["max"] > 0.01:
            put("error_increase", value["ratio"], signal.support)
        elif value["baseline_mean"] == 0 and value["recent_mean"] >= ERROR_RATIO_HIGH:
            put("error_increase", None, signal.support)
    for signal in table.named("metric_p95_latency"):
        value = signal.value
        if value["max"] >= P95_HIGH_SECONDS:
            put("p95_high", value["max"], signal.support)
        if value["ratio"] is not None and value["ratio"] >= INCREASE_RATIO:
            put("p95_increase", value["ratio"], signal.support)
    return found


def detect(table: SignalTable, families: list[FamilyAssessment]) -> Detection:
    found = features(table)
    eligible = {item.family: item for item in families}
    hypotheses = []
    for family in FAMILIES:
        score = 0.0
        used: list[tuple[str, float]] = []
        support: set[EntryRef] = set()
        for name, weight in WEIGHTS[family].items():
            if name in found:
                score += weight
                used.append((name, weight))
                if weight > 0:
                    support.update(found[name].support)
        assessment = eligible.get(family)
        missing = (
            [slot.slot for slot in assessment.required_slots if not slot.usable]
            if assessment
            else []
        )
        hypotheses.append(
            Hypothesis(
                family=family,
                score=score,
                features=used,
                support=tuple(sorted(support)),
                eligible=bool(assessment and assessment.eligible_for_assessment),
                missing_required_slots=missing,
            )
        )
    hypotheses.sort(key=lambda item: (-item.score, FAMILIES.index(item.family)))
    top = hypotheses[0]
    second = hypotheses[1]
    if top.score < THRESHOLD:
        return Detection("unknown", "below_threshold", hypotheses)
    if top.score - second.score < MARGIN:
        return Detection("unknown", "ambiguous_margin", hypotheses)
    if not top.eligible:
        return Detection("unknown", "top_family_ineligible", hypotheses)
    return Detection(top.family, "detected", hypotheses)
