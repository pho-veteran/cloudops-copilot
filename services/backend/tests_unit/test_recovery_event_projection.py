import pytest

from cloudops.features.investigations.kubernetes_collector import _event_summary


@pytest.mark.parametrize(
    "fields,count,last",
    [
        ({"count": 3, "lastTimestamp": "2026-11-16T12:00:03Z"}, 3, "2026-11-16T12:00:03Z"),
        ({"eventTime": "2026-11-16T12:00:01Z"}, 1, "2026-11-16T12:00:01Z"),
        (
            {
                "eventTime": "2026-11-16T12:00:01Z",
                "series": {"count": 4, "lastObservedTime": "2026-11-16T12:00:04Z"},
            },
            4,
            "2026-11-16T12:00:04Z",
        ),
        ({}, None, None),
        ({"eventTime": "2026-11-16T12:00:01Z", "count": True}, None, "2026-11-16T12:00:01Z"),
        ({"eventTime": "2026-11-16T12:00:01Z", "series": {}}, None, "2026-11-16T12:00:01Z"),
    ],
)
def test_event_projection_retains_legacy_and_modern_occurrence_semantics(fields, count, last):
    value = _event_summary({"metadata": {"uid": "event", "namespace": "demo"}, **fields})
    assert value.get("count") == count and value.get("last_seen") == last
