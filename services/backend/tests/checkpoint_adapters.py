"""Labeled test-only source adapters for checkpoint durability tests.

They are registered by tests (or a test subprocess) only; production registration stays finite.
"""

import os
import time
from datetime import datetime, timezone

from cloudops.features.investigations import collector
from cloudops.features.investigations.schemas import Coverage, SourceResult, SourceTimes

CALLS: dict[str, int] = {}


def _result(key, kind, payload):
    now = datetime.now(timezone.utc)
    return SourceResult(
        source_key=key,
        payload_kind=kind,
        payload_version=1,
        coverage=Coverage(status="available", reason_codes=[]),
        source_times=SourceTimes(
            observed_at=now, collected_at=now, time_relation="current_observation"
        ),
        redacted_bounded_payload=payload,
    )


def state(context, _timeout):
    CALLS["kubernetes.state"] = CALLS.get("kubernetes.state", 0) + 1
    pods = [{"uid": f"pod-{i:02}", "name": f"checkout-{i:02}"} for i in range(12)]
    return _result(
        "kubernetes.state",
        "state",
        {
            "deployment": {"name": context.scope_snapshot.name, "uid": context.workload_uid},
            "pods": pods,
        },
    )


def events(_context, _timeout):
    CALLS["kubernetes.events"] = CALLS.get("kubernetes.events", 0) + 1
    return _result("kubernetes.events", "events", {"events": [{"uid": "e-1", "reason": "BackOff"}]})


def logs(_context, _timeout):
    CALLS["kubernetes.logs.current"] = CALLS.get("kubernetes.logs.current", 0) + 1
    pods = [
        {"uid": f"pod-{i:02}", "containers": [{"name": "app", "lines": [f"line {i}"]}]}
        for i in (0, 11)
    ]
    return _result("kubernetes.logs.current", "logs", {"pods": pods})


def fixture(context, timeout):
    CALLS["development_fixture"] = CALLS.get("development_fixture", 0) + 1
    return collector.development_fixture(context, timeout)


ADAPTERS = {
    "kubernetes.state": state,
    "kubernetes.events": events,
    "kubernetes.logs.current": logs,
    "development_fixture": fixture,
}


def request_rate(_context, _timeout):
    return _result(
        "prometheus.request_rate",
        "series",
        {"series": [{"samples": [[i, 1] for i in range(12000)]}]},
    )


def error_ratio(_context, _timeout):
    return _result(
        "prometheus.error_ratio",
        "series",
        {"series": [{"samples": [[i, 2] for i in range(12000)]}]},
    )


# Deliberately reverse canonical order: the shared 20,000-sample budget must not
# depend on adapter registration or catalog insertion order.
METRIC_ADAPTERS = {
    "prometheus.request_rate": request_rate,
    "prometheus.error_ratio": error_ratio,
}


def install(registry=None, keys=tuple(ADAPTERS)):
    """Register the labeled adapters and return the configured binding for ``keys``."""
    registry = collector.REGISTERED_ADAPTERS if registry is None else registry
    binding = {key: ADAPTERS[key] for key in keys}
    delayed_key = os.environ.get("CHECKPOINT_DELAY_SOURCE")
    delay = float(os.environ.get("CHECKPOINT_FIXTURE_DELAY", "0"))
    if delay:
        original = binding[delayed_key]

        def delayed(context, timeout):
            # Test-only process-loss pause, before invoking the selected fixture.
            time.sleep(delay)
            return original(context, timeout)

        binding[delayed_key] = delayed
    registry.update(binding)
    return binding
