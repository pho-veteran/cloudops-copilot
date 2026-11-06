"""Pure projection invariants; these are stored contract fixtures, not live telemetry."""

from copy import deepcopy

import pytest

from cloudops.features.investigations.locators import UnsupportedProjection, project, timestamp


def views(kind, data):
    return [entry.view().model_dump() for entry in project(kind, 1, data)]


def test_field_identity_survives_pod_and_object_reordering_without_mutation():
    first = {
        "pods": [
            {"uid": "pod-a", "name": "a", "restart_count": 2},
            {"uid": "pod-b", "restart_count": 0},
        ]
    }
    original = deepcopy(first)
    second = {"pods": list(reversed(first["pods"]))}
    assert views("state", first) == views("state", second)
    assert first == original
    changed = deepcopy(first)
    changed["pods"][0]["restart_count"] = 3
    before, after = project("state", 1, first), project("state", 1, changed)
    assert [e.key for e in before] == [e.key for e in after]
    assert [e.value for e in before] != [e.value for e in after]


def test_duplicate_conflicting_event_uid_is_disclosed_not_arbitrarily_resolved():
    a, b = (
        {"uid": "event-a", "count": 1, "reason": "ReasonA"},
        {"uid": "event-a", "count": 2, "reason": "ReasonB"},
    )
    first = views("events", {"events": [a, b]})
    assert first == views("events", {"events": [b, a]})
    assert first[0]["ambiguous"] and first[0]["multiplicity"] == 2


def test_identical_events_have_one_stable_target_with_multiplicity():
    event = {"reason": "FailedScheduling", "message": "Original human text"}
    entries = views("events", {"events": [event, event]})
    assert len(entries) == 1 and entries[0]["multiplicity"] == 2
    assert not entries[0]["ambiguous"]


def test_log_targets_are_scoped_by_uid_container_previous_and_content():
    current = {"pod_uid": "pod-a", "container": "app", "previous": False, "lines": ["Kiểm tra log"]}
    previous = {**current, "previous": True}
    result = views("logs", {"logs": [current, previous, current]})
    assert len(result) == 2 and sorted(row["multiplicity"] for row in result) == [1, 2]
    assert result == views("logs", {"logs": [previous, current, current]})
    assert views("logs", {"logs": [{"container": "app", "text": "No Pod UID"}]}) == []


def test_series_sample_targets_normalize_times_and_detect_conflicting_duplicates():
    entries = project(
        "series",
        1,
        {
            "series": [
                {
                    "labels": {"pod_uid": "pod-a"},
                    "unit": "bytes",
                    "samples": [[1, 0], ["1.0", 2], [2, 5]],
                }
            ]
        },
    )
    samples = [entry for entry in entries if entry.kind == "sample"]
    assert len(samples) == 2
    assert sum(entry.ambiguous for entry in samples) == 1
    assert timestamp("2026-11-06T00:00:00Z") == timestamp("2026-11-06T07:00:00+07:00")
    assert timestamp(True) is None and timestamp("NaN") is None


def test_previews_bound_utf8_without_mutating_retained_content():
    payload = {"message": "ữ" * 3000}
    result = views("state", payload)[0]
    assert result["preview_truncated"]
    assert len(result["value"].encode()) < 1024
    assert payload["message"] == "ữ" * 3000


@pytest.mark.parametrize("kind,version", [("state", 2), ("arbitrary", 1)])
def test_unsupported_payload_is_not_guessed(kind, version):
    with pytest.raises(UnsupportedProjection):
        project(kind, version, {})
