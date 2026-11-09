"""Stable source slots and evidence coverage for the standard catalog."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from cloudops.features.investigations.schemas import Coverage

CATALOG_VERSION = "standard_v1"


@dataclass(frozen=True)
class SourceSpec:
    source_kind: str
    payload_kind: str
    byte_limit: int
    query_id: str | None = None


SOURCES = {
    "kubernetes.state": SourceSpec("state", "state", 1024 * 1024),
    "kubernetes.events": SourceSpec("events", "events", 512 * 1024),
    "kubernetes.logs.current": SourceSpec("logs", "logs", 2 * 1024 * 1024),
    "kubernetes.logs.previous": SourceSpec("logs", "logs", 2 * 1024 * 1024),
    "kubernetes.configmap.metadata": SourceSpec("state", "state", 256 * 1024),
    **{
        f"prometheus.{query}": SourceSpec("series", "series", 256 * 1024, query)
        for query in (
            "request_rate",
            "error_ratio",
            "p95_latency",
            "memory_working_set",
            "memory_limit",
            "restarts",
        )
    },
    "development_fixture": SourceSpec("fixture", "fixture", 256 * 1024),
}

_STATE = "kubernetes.state"
_EVENTS = "kubernetes.events"
_LOGS = ("kubernetes.logs.current", "kubernetes.logs.previous")
_METRICS = tuple(key for key in SOURCES if key.startswith("prometheus."))
_CONFIG = "kubernetes.configmap.metadata"
# Logs are required where applicable for crash/OOM; an explicit not_applicable
# outcome is the only proof that a conditional log is not required.
_PACKS = (
    ("crashloop_startup", (_STATE, _EVENTS, *_LOGS), (*_METRICS, _CONFIG)),
    ("oom_restarts", (_STATE, _EVENTS, *_LOGS), (*_METRICS, _CONFIG)),
    ("traffic_latency_errors", _METRICS[:3], (_STATE, _EVENTS, *_LOGS, _CONFIG, *_METRICS[3:])),
    ("pending_unschedulable", (_STATE, _EVENTS), (*_LOGS, _CONFIG, *_METRICS)),
    ("image_pull", (_STATE, _EVENTS), (*_LOGS, _CONFIG, *_METRICS)),
)


def derive_pack_coverage(outcomes: Mapping[str, "Coverage"]) -> list[dict]:
    packs = []
    for pack, required, optional in _PACKS:
        missing = []
        usable = 0
        for key in required:
            coverage = outcomes.get(key)
            status = coverage.status if coverage else "unavailable"
            truncated_log = (
                key in _LOGS
                and status == "partial"
                and coverage is not None
                and coverage.reason_codes == ["truncated"]
            )
            if status in ("available", "partial"):
                usable += 1
            if (
                status != "available"
                and not truncated_log
                and not (key in _LOGS and status == "not_applicable")
            ):
                missing.append(key)
        gaps = [
            key
            for key in optional
            if key not in outcomes
            or (
                outcomes[key].status != "available"
                and not (key in _LOGS and outcomes[key].status == "not_applicable")
            )
        ]
        status = "available" if not missing else "partial" if usable else "unavailable"
        packs.append(dict(pack=pack, status=status, missing_required=missing, optional_gaps=gaps))
    return packs
