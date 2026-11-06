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
    delay = float(os.environ.get("CHECKPOINT_FIXTURE_DELAY", "0"))
    if delay:
        time.sleep(delay)
    return collector.development_fixture(context, timeout)


ADAPTERS = {
    "kubernetes.state": state,
    "kubernetes.events": events,
    "kubernetes.logs.current": logs,
    "development_fixture": fixture,
}


def install(registry=None, keys=tuple(ADAPTERS)):
    """Register the labeled adapters and return the configured binding for ``keys``."""
    registry = collector.REGISTERED_ADAPTERS if registry is None else registry
    for key in keys:
        registry[key] = ADAPTERS[key]
    return {key: ADAPTERS[key] for key in keys}
