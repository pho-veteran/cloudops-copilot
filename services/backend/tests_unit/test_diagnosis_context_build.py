from __future__ import annotations

import pytest

from cloudops.features.diagnosis import versions


def test_versions_are_unique_and_pinned():
    values = {
        versions.CONTEXT_VERSION: "e06.context/v1",
        versions.TARGET_RULES_VERSION: "e06.targets/v1",
        versions.PROMPT_VERSION: "e06.rca_prompt/v1",
        versions.VALIDATOR_VERSION: "e06.validator/v1",
        versions.PROBE_PROMPT_VERSION: "e06.probe_prompt/v1",
    }
    assert list(values.values()) == [
        "e06.context/v1",
        "e06.targets/v1",
        "e06.rca_prompt/v1",
        "e06.validator/v1",
        "e06.probe_prompt/v1",
    ]
    for value in values:
        assert value.startswith("e06.")
        assert value.endswith("/v1")
    assert len(values) == 5


def test_alias_and_reserve_bounds_match_the_advice_contract():
    assert versions.ALIAS_CAPACITY == 999
    assert versions.REPAIR_RESERVE_BYTES == 4096
    assert versions.MAX_FEEDBACK_BYTES == 2048
    assert versions.MAX_FEEDBACK_BYTES <= versions.REPAIR_RESERVE_BYTES
    assert versions.MAX_FEEDBACK_FAILURES == 10


def test_family_matrix_is_the_d2_table_in_fixed_order():
    families = versions.FAMILY_REQUIRED
    assert list(families) == [
        "crashloop_startup",
        "oom_restarts",
        "traffic_latency_errors",
        "pending_unschedulable",
        "image_pull",
    ]
    crashloop = (
        "kubernetes.state",
        "kubernetes.events",
        "kubernetes.logs.current",
        "kubernetes.logs.previous",
    )
    assert families["crashloop_startup"] == crashloop
    assert families["oom_restarts"] == crashloop
    assert families["traffic_latency_errors"] == (
        "prometheus.request_rate",
        "prometheus.error_ratio",
        "prometheus.p95_latency",
    )
    assert families["pending_unschedulable"] == ("kubernetes.state", "kubernetes.events")
    assert families["image_pull"] == ("kubernetes.state", "kubernetes.events")


def test_every_slot_has_a_source_class_and_cap():
    assert set(versions.SOURCE_CLASSES) == set(versions.SLOT_CAPS)
    assert versions.SOURCE_CLASSES["kubernetes.configmap.metadata"] == "kubernetes_state"
    assert versions.SOURCE_CLASSES["kubernetes.logs.previous"] == "container_logs"
    assert versions.SOURCE_CLASSES["prometheus.restarts"] == "prometheus_series"
    assert versions.SOURCE_CLASSES["development_fixture"] == "synthetic_fixture"
    assert versions.SLOT_CAPS["development_fixture"][0] == 0
    for required_cap, optional_cap, order in versions.SLOT_CAPS.values():
        assert isinstance(required_cap, int) and required_cap >= 0
        assert isinstance(optional_cap, int) and optional_cap >= 0
        assert order in {"key_asc", "newest_first", "group_newest"}
    assert versions.SLOT_CAPS["kubernetes.events"] == (50, 20, "newest_first")
    assert versions.SLOT_CAPS["kubernetes.logs.current"] == (3, 1, "group_newest")


@pytest.mark.parametrize("family", list(versions.FAMILY_REQUIRED))
def test_family_required_slots_are_known_slots(family):
    assert set(versions.FAMILY_REQUIRED[family]) <= set(versions.SLOT_CAPS)
