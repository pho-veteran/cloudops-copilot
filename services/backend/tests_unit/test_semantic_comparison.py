"""Comparison policy over persisted source-shaped contract fixtures."""

from types import SimpleNamespace
from uuid import uuid4

from cloudops.features.investigations.comparison import differences
from cloudops.features.investigations.comparison_service import page
from cloudops.features.investigations.locators import project
from cloudops.shared.errors import DomainError
import pytest


def meta(kind="state", coverage="available", query=None, window=None):
    if kind == "series":
        query = query or {
            "query_id": "request_rate",
            "query_version": "fixture_v1",
            "unit": "bytes",
            "source_identity": {"endpoint_id": "fixture"},
            "step": 30,
        }
        window = window or "same-window"
    return SimpleNamespace(
        id=uuid4(),
        run_id=uuid4(),
        payload_kind=kind,
        payload_version=1,
        coverage=SimpleNamespace(status=coverage),
        query_metadata=query,
        requested_window=window,
    )


def test_partial_absence_is_not_a_removed_or_zero_value():
    left = {"deployment": {"replicas": 2, "ready_replicas": 0}}
    rows = list(
        differences(meta(), meta(coverage="partial"), left, {"deployment": {"replicas": 2}})
    )
    missing = next(row for row in rows if row.label.endswith("ready_replicas"))
    assert missing.status == "unavailable" and missing.left == 0 and missing.right is None
    assert missing.left_citation and missing.right_citation is None


def test_complete_membership_change_is_added_and_swap_is_removed():
    before, after = {"pods": []}, {"pods": [{"uid": "pod-a", "name": "a"}]}
    forward = list(differences(meta(), meta(), before, after))
    backward = list(differences(meta(), meta(), after, before))
    assert {row.status for row in forward} == {"added"}
    assert {row.status for row in backward} == {"removed"}
    assert [row.key for row in forward] == [row.key for row in backward]


@pytest.mark.parametrize("change", ["window", "unit", "coverage", "query"])
def test_incompatible_numeric_context_does_not_align_samples(change):
    query = {
        "query_id": "request_rate",
        "query_version": "fixture_v1",
        "unit": "rps",
        "step": 30,
        "source_identity": {"endpoint_id": "fixture"},
    }
    left, right = (
        meta("series", query=query, window="same"),
        meta("series", query=query.copy(), window="same"),
    )
    payload = {
        "series": [{"labels": {"pod_uid": "a"}, "unit": "rps", "samples": [[1, 0], [31, 3]]}]
    }
    if change == "window":
        right.requested_window = "different"
    elif change == "unit":
        right.query_metadata["unit"] = "bytes"
    elif change == "query":
        right.query_metadata["step"] = 60
    else:
        right.coverage.status = "partial"
    samples = [row for row in differences(left, right, payload, payload) if row.kind == "sample"]
    assert len(samples) == 2 and {row.status for row in samples} == {"not_comparable"}


def test_missing_samples_are_not_interpolated_or_filled():
    left, right = meta("series"), meta("series")
    a = {"series": [{"unit": "bytes", "samples": [[1, 0]]}]}
    b = {"series": [{"unit": "bytes", "samples": [[2, 2]]}]}
    rows = [row for row in differences(left, right, a, b) if row.kind == "sample"]
    assert {row.status for row in rows} == {"added", "removed"}
    assert all(row.left is None or row.right is None for row in rows)


def test_ambiguous_duplicate_key_is_not_a_changed_numeric_observation():
    a = {"series": [{"unit": "bytes", "samples": [[1, 0], [1, 3]]}]}
    rows = list(differences(meta("series"), meta("series"), a, a))
    assert next(row for row in rows if row.kind == "sample").reason == "ambiguous_identity"


def test_byte_budget_pages_without_losing_or_duplicating_targets():
    entries = project("state", 1, {f"field-{i:03}": "ữ" * 3000 for i in range(150)})
    expected = [entry.key for entry in entries]
    actual, cursor = [], None
    while True:
        rows, cursor = page((entry.view() for entry in entries), "snapshot", cursor, 100)
        assert sum(len(row.model_dump_json().encode()) for row in rows) <= 128 * 1024
        actual.extend(row.locator.key for row in rows)
        if not cursor:
            break
    assert actual == expected


def test_cursor_is_not_an_authority_and_does_not_survive_snapshot_change():
    entries = project("fixture", 1, {"label": "fixture", "observation": "stored"})
    _, cursor = page((entry.view() for entry in entries), "old", None, 1)
    with pytest.raises(DomainError) as changed:
        page((entry.view() for entry in entries), "new", cursor, 1)
    assert changed.value.status == 409


def test_numeric_values_without_units_or_query_provenance_are_not_aligned():
    left, right = meta("series"), meta("series")
    left.query_metadata = right.query_metadata = None
    payload = {"series": [{"labels": {"pod_uid": "pod-a"}, "samples": [[1, 0]]}]}
    samples = [row for row in differences(left, right, payload, payload) if row.kind == "sample"]
    assert len(samples) == 1 and samples[0].status == "not_comparable"


def test_inconsistent_embedded_deployment_uid_does_not_compare_replicas():
    left = {"deployment": {"uid": "a", "replicas": 1}}
    right = {"deployment": {"uid": "b", "replicas": 2}}
    rows = list(differences(meta(), meta(), left, right))
    assert rows and {row.reason for row in rows} == {"payload_identity_changed"}


@pytest.mark.parametrize("encoded", [1.0, "1.0", "1e0"])
def test_equal_numeric_samples_keep_raw_values_without_a_false_difference(encoded):
    left = {"series": [{"unit": "bytes", "samples": [[1, 1]]}]}
    right = {"series": [{"unit": "bytes", "samples": [[1, encoded]]}]}
    rows = [
        row
        for row in differences(meta("series"), meta("series"), left, right)
        if row.kind == "sample"
    ]
    assert len(rows) == 1 and rows[0].status == "unchanged"
    assert rows[0].left == 1
    assert type(rows[0].right) is type(encoded) and rows[0].right == encoded


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", None, True])
def test_invalid_numeric_samples_are_never_comparable(value):
    payload = {"series": [{"unit": "bytes", "samples": [[1, value]]}]}
    rows = [
        row
        for row in differences(meta("series"), meta("series"), payload, payload)
        if row.kind == "sample"
    ]
    assert len(rows) == 1 and rows[0].status == "not_comparable"


def test_numeric_coercion_does_not_change_state_field_semantics():
    rows = list(differences(meta(), meta(), {"replicas": 1}, {"replicas": "1.0"}))
    assert len(rows) == 1 and rows[0].status == "changed"
