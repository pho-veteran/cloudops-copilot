"""Fixed identities and bounds for the scoped diagnosis context (`e06.*/v1`).

Pure constants: no imports from other diagnosis modules and no side effects. Every
selection, budget decision and validation result is reproducible from these values.
"""

from __future__ import annotations

CONTEXT_VERSION = "e06.context/v1"
TARGET_RULES_VERSION = "e06.targets/v1"
PROMPT_VERSION = "e06.rca_prompt/v2"
VALIDATOR_VERSION = "e06.validator/v1"
PROBE_PROMPT_VERSION = "e06.probe_prompt/v1"

# Three-digit `E`/`K` alias space in `RCAAdvice/v1`.
ALIAS_CAPACITY = 999
# Optional material leaves room for one bounded structural repair.
REPAIR_RESERVE_BYTES = 4096
MAX_FEEDBACK_BYTES = 2048
MAX_FEEDBACK_FAILURES = 10

# slot -> (required cap, optional cap, ordering rule)
SLOT_CAPS: dict[str, tuple[int, int, str]] = {
    "kubernetes.state": (250, 250, "key_asc"),
    "kubernetes.configmap.metadata": (250, 250, "key_asc"),
    "kubernetes.events": (50, 20, "newest_first"),
    "kubernetes.logs.current": (3, 1, "group_newest"),
    "kubernetes.logs.previous": (3, 1, "group_newest"),
    "prometheus.request_rate": (250, 250, "key_asc"),
    "prometheus.error_ratio": (250, 250, "key_asc"),
    "prometheus.p95_latency": (250, 250, "key_asc"),
    "prometheus.memory_working_set": (250, 250, "key_asc"),
    "prometheus.memory_limit": (250, 250, "key_asc"),
    "prometheus.restarts": (250, 250, "key_asc"),
    "development_fixture": (0, 20, "key_asc"),
}

SOURCE_CLASSES: dict[str, str] = {
    "kubernetes.state": "kubernetes_state",
    "kubernetes.configmap.metadata": "kubernetes_state",
    "kubernetes.events": "kubernetes_events",
    "kubernetes.logs.current": "container_logs",
    "kubernetes.logs.previous": "container_logs",
    "prometheus.request_rate": "prometheus_series",
    "prometheus.error_ratio": "prometheus_series",
    "prometheus.p95_latency": "prometheus_series",
    "prometheus.memory_working_set": "prometheus_series",
    "prometheus.memory_limit": "prometheus_series",
    "prometheus.restarts": "prometheus_series",
    "development_fixture": "synthetic_fixture",
}

# D2 required-source table, in fixed family order.
FAMILY_REQUIRED: dict[str, tuple[str, ...]] = {
    "crashloop_startup": (
        "kubernetes.state",
        "kubernetes.events",
        "kubernetes.logs.current",
        "kubernetes.logs.previous",
    ),
    "oom_restarts": (
        "kubernetes.state",
        "kubernetes.events",
        "kubernetes.logs.current",
        "kubernetes.logs.previous",
    ),
    "traffic_latency_errors": (
        "prometheus.request_rate",
        "prometheus.error_ratio",
        "prometheus.p95_latency",
    ),
    "pending_unschedulable": ("kubernetes.state", "kubernetes.events"),
    "image_pull": ("kubernetes.state", "kubernetes.events"),
}
