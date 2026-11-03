"""Work queue search, filtering, sorting and stable pagination."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from cloudops.features.access.models import User
from cloudops.features.access.service import provision
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.incidents.service import list_incidents
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now


@pytest.fixture
def actor_id(db):
    provision(db, "queue-operator", "local-test-password", "operator")
    return db.scalar(select(User.id).where(User.username == "queue-operator"))


def create_workload(db, name: str) -> Workload:
    workload = Workload(
        cluster="queue-test",
        namespace="default",
        name=name,
        uid=f"queue-test-{name}",
    )
    db.add(workload)
    db.flush()
    return workload


def create_incident(db, workload_id, actor_id, title: str, **fields) -> Incident:
    incident = Incident(
        workload_id=workload_id,
        title=title,
        description="",
        created_by=actor_id,
        **fields,
    )
    db.add(incident)
    db.flush()
    return incident


def test_search_by_title_case_insensitive(db, actor_id):
    """Search finds incidents by partial title match, case-insensitive."""
    workload = create_workload(db, "search")
    create_incident(db, workload.id, actor_id, title="Database connection timeout")
    create_incident(db, workload.id, actor_id, title="API rate limit exceeded")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, search="database")

    assert len(result.items) == 1
    assert "Database" in result.items[0].title


def test_search_by_uuid_prefix(db, actor_id):
    """Search finds incidents by UUID prefix (first 8 chars)."""
    workload = create_workload(db, "uuid-prefix")
    incident = create_incident(db, workload.id, actor_id, title="Test incident")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, search=str(incident.id)[:8])

    assert len(result.items) == 1
    assert result.items[0].id == incident.id


def test_search_does_not_match_uuid_in_the_middle(db, actor_id):
    """UUID search is a prefix match, not a substring match."""
    workload = create_workload(db, "uuid-middle")
    incident = create_incident(db, workload.id, actor_id, title="Test incident")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, search=str(incident.id)[8:16])

    assert result.items == []


def test_search_treats_wildcards_literally(db, actor_id):
    """A percent or underscore in the term never acts as a SQL wildcard."""
    workload = create_workload(db, "wildcards")
    create_incident(db, workload.id, actor_id, title="Disk at 100% capacity")
    create_incident(db, workload.id, actor_id, title="Disk at 100x capacity")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, search="100%")

    assert [item.title for item in result.items] == ["Disk at 100% capacity"]


def test_filter_by_workload(db, actor_id):
    """Filter returns only incidents for the specified workload."""
    workload_a = create_workload(db, "workload-a")
    workload_b = create_workload(db, "workload-b")
    create_incident(db, workload_a.id, actor_id, title="A incident")
    create_incident(db, workload_b.id, actor_id, title="B incident")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, filters={"workload_id": workload_a.id})

    assert len(result.items) == 1
    assert result.items[0].workload_id == workload_a.id


def test_filters_combine(db, actor_id):
    """Origin, state, severity and alert signal narrow the queue together."""
    workload = create_workload(db, "combined")
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Matching",
        state="open",
        severity="critical",
        alert_signal="firing",
    )
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Resolved",
        state="resolved",
        severity="critical",
        alert_signal="firing",
    )
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Quiet",
        state="open",
        severity="critical",
        alert_signal="none",
    )
    db.commit()

    result = list_incidents(
        db,
        limit=25,
        offset=0,
        filters={"state": "open", "severity": "critical", "alert_signal": "firing"},
    )

    assert [item.title for item in result.items] == ["Matching"]


def test_filter_unknown_workload_is_404(db, actor_id):
    """An unknown well-formed workload identity is a domain error, never an empty queue."""
    from cloudops.shared.errors import DomainError

    with pytest.raises(DomainError) as caught:
        list_incidents(db, limit=25, offset=0, filters={"workload_id": uuid4()})

    assert caught.value.status == 404
    assert caught.value.code == "workload_not_found"


def test_sort_by_severity_nulls_last(db, actor_id):
    """Severity sort is alphabetical, descending, with nulls last."""
    workload = create_workload(db, "severity")
    create_incident(db, workload.id, actor_id, title="No severity", severity=None)
    create_incident(db, workload.id, actor_id, title="Critical", severity="critical")
    create_incident(db, workload.id, actor_id, title="Warning", severity="warning")
    db.commit()

    result = list_incidents(db, limit=25, offset=0, sort_by="severity", sort_order="desc")

    severities = [item.severity.value for item in result.items]
    assert severities == ["warning", "critical", None]


def test_sort_by_first_seen_nulls_last(db, actor_id):
    """Nullable timestamp sorts keep unknown seen times at the end."""
    workload = create_workload(db, "seen")
    now = database_now(db)
    create_incident(db, workload.id, actor_id, title="Unknown")
    create_incident(
        db, workload.id, actor_id, title="Older", first_seen_at=now - timedelta(hours=1)
    )
    create_incident(db, workload.id, actor_id, title="Newer", first_seen_at=now)
    db.commit()

    result = list_incidents(db, limit=25, offset=0, sort_by="first_seen_at", sort_order="desc")

    assert [item.title for item in result.items] == ["Newer", "Older", "Unknown"]


def test_sort_rejects_unknown_column(db, actor_id):
    """An unknown sort key is a programming error, not an arbitrary column."""
    with pytest.raises(ValueError):
        list_incidents(db, limit=25, offset=0, sort_by="description")


def test_pagination_stable_with_id_tiebreak(db, actor_id):
    """Equal sort keys fall back to an ascending ID and repeated pages are identical."""
    workload = create_workload(db, "pagination")
    created_at = database_now(db)
    for index in range(55):
        create_incident(
            db,
            workload.id,
            actor_id,
            title=f"Incident {index:02d}",
            created_at=created_at,
            severity=None if index % 2 else "warning",
        )
    db.commit()

    first = list_incidents(db, limit=25, offset=0)
    repeated = list_incidents(db, limit=25, offset=0)
    second = list_incidents(db, limit=25, offset=first.next_offset)

    assert first.items[0].id == repeated.items[0].id
    assert first.items[-1].id == repeated.items[-1].id
    first_ids = [item.id for item in first.items]
    assert first_ids == sorted(first_ids)
    assert first.next_offset == 25
    assert set(first_ids).isdisjoint({item.id for item in second.items})


def test_microsecond_anchor_preserves_unchanged_pages(db, actor_id):
    workload = create_workload(db, "precise-anchor")
    timestamp = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=UTC)
    expected = {
        create_incident(
            db,
            workload.id,
            actor_id,
            title=f"Precise {index}",
            created_at=timestamp - timedelta(microseconds=index),
        ).id
        for index in range(55)
    }
    db.commit()
    initial = list_incidents(db, limit=25, offset=0, sort_by="created_at")
    anchor = initial.creation_anchor
    assert anchor.created_at == timestamp
    found = []
    offset = 0
    while offset is not None:
        page = list_incidents(
            db,
            limit=25,
            offset=offset,
            sort_by="created_at",
            anchor_created_at=anchor.created_at,
            anchor_id=anchor.id,
        )
        found.extend(item.id for item in page.items)
        offset = page.next_offset
    assert len(found) == len(expected)
    assert set(found) == expected


def test_null_sort_values_paginate_without_skips(db, actor_id):
    """Half-null severity data across 60 incidents still pages every row exactly once."""
    workload = create_workload(db, "nulls")
    for index in range(60):
        create_incident(
            db,
            workload.id,
            actor_id,
            title=f"Incident {index:02d}",
            severity=None if index % 2 else ("warning" if index % 4 else "critical"),
        )
    db.commit()

    seen = []
    offset = 0
    while offset is not None:
        page = list_incidents(db, limit=25, offset=offset, sort_by="severity", sort_order="desc")
        seen.extend(item.id for item in page.items)
        offset = page.next_offset

    assert len(seen) == 60
    assert len(set(seen)) == 60
    ordered = list_incidents(db, limit=100, offset=0, sort_by="severity", sort_order="desc")
    severities = [item.severity.value for item in ordered.items]
    assert None not in severities[:30]
    assert set(severities[30:]) == {None}


def configured_workload(db) -> Workload:
    return db.scalar(select(Workload).order_by(Workload.id))


def test_list_endpoint_defaults_to_current_incidents_by_last_seen(client, db, actor_id, seeded):
    """The default queue shows open/acknowledged work ordered by latest seen time."""
    from conftest import sign_in

    workload = configured_workload(db)
    now = database_now(db)
    create_incident(
        db,
        workload.id,
        actor_id,
        title="One",
        created_at=now - timedelta(minutes=1),
        first_seen_at=now - timedelta(minutes=1),
        last_seen_at=now - timedelta(minutes=1),
    )
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Two",
        created_at=now,
        first_seen_at=now,
        last_seen_at=now,
        state="acknowledged",
    )
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Resolved",
        created_at=now + timedelta(seconds=1),
        state="resolved",
    )
    db.commit()

    response = client.get("/api/incidents", headers=sign_in(client))

    assert response.status_code == 200
    body = response.json()
    assert [item["title"] for item in body["items"]] == ["Two", "One"]
    assert body["next_offset"] is None
    assert body["creation_anchor"]["id"]

    all_states = client.get("/api/incidents?view=all", headers=sign_in(client)).json()
    assert [item["title"] for item in all_states["items"]] == ["Two", "One", "Resolved"]


def test_list_endpoint_unknown_workload_is_404(client, seeded):
    from conftest import sign_in

    response = client.get(f"/api/incidents?workload_id={uuid4()}", headers=sign_in(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workload_not_found"


def test_list_endpoint_malformed_workload_is_422(client, seeded):
    from conftest import sign_in

    response = client.get("/api/incidents?workload_id=not-a-uuid", headers=sign_in(client))

    assert response.status_code == 422


def test_list_endpoint_limit_bounds_unchanged(client, seeded):
    from conftest import sign_in

    headers = sign_in(client)
    assert client.get("/api/incidents?limit=101", headers=headers).status_code == 422
    assert client.get("/api/incidents?limit=0", headers=headers).status_code == 422
    assert client.get("/api/incidents?offset=-1", headers=headers).status_code == 422


def test_list_endpoint_rejects_unknown_sort(client, seeded):
    from conftest import sign_in

    response = client.get("/api/incidents?sort_by=description", headers=sign_in(client))

    assert response.status_code == 422


def test_list_endpoint_rejects_unknown_run_state(client, seeded):
    from conftest import sign_in

    response = client.get("/api/incidents?run_state=unknown", headers=sign_in(client))

    assert response.status_code == 422


def test_viewer_can_search_filter_and_sort(client, db, actor_id, seeded):
    from conftest import sign_in

    workload = configured_workload(db)
    create_incident(
        db,
        workload.id,
        actor_id,
        title="Database timeout",
        state="open",
        severity="critical",
        alert_signal="firing",
    )
    create_incident(db, workload.id, actor_id, title="Disk pressure", state="resolved")
    db.commit()

    response = client.get(
        "/api/incidents?search=database&state=open&severity=critical"
        "&alert_signal=firing&sort_by=severity&sort_order=asc",
        headers=sign_in(client, "viewer"),
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["title"] for item in items] == ["Database timeout"]


def test_list_endpoint_applies_origin_and_workload_filters(client, db, actor_id, seeded):
    from conftest import sign_in

    workload = configured_workload(db)
    other = create_workload(db, "endpoint-other")
    create_incident(db, workload.id, actor_id, title="Configured workload")
    create_incident(db, other.id, actor_id, title="Other workload")
    db.commit()

    headers = sign_in(client)
    by_workload = client.get(f"/api/incidents?workload_id={workload.id}", headers=headers)
    by_origin = client.get("/api/incidents?origin=operator", headers=headers)

    assert [item["title"] for item in by_workload.json()["items"]] == ["Configured workload"]
    assert {item["title"] for item in by_origin.json()["items"]} == {
        "Configured workload",
        "Other workload",
    }


def test_blocked_view_keeps_historical_failed_run_with_newer_success(db, actor_id):
    workload = create_workload(db, "historical-run-blocked")
    incident = create_incident(db, workload.id, actor_id, title="Recovered after failure")
    now = database_now(db)
    db.add_all(
        [
            InvestigationRun(
                incident_id=incident.id,
                state="failed",
                requested_by=actor_id,
                created_at=now - timedelta(minutes=1),
            ),
            InvestigationRun(
                incident_id=incident.id,
                state="completed",
                requested_by=actor_id,
                created_at=now,
            ),
        ]
    )
    db.commit()

    result = list_incidents(db, 25, 0, view="blocked")

    assert [item.title for item in result.items] == ["Recovered after failure"]
    assert result.items[0].latest_run.state == "completed"
    assert result.items[0].run_blockers == ["failed"]


def test_resolved_incident_retains_all_unknown_and_active_execution_warnings(db, actor_id):
    workload = create_workload(db, "execution-warnings")
    incident = create_incident(
        db, workload.id, actor_id, title="Resolved with unresolved outcome", state="resolved"
    )
    now = database_now(db)
    unknown_plan = ActionPlan(
        incident_id=incident.id,
        workload_id=workload.id,
        content={},
        digest="a" * 64,
        target_epoch="epoch-unknown",
        rationale="test evidence",
        created_by=actor_id,
        expires_at=now + timedelta(hours=1),
    )
    active_plan = ActionPlan(
        incident_id=incident.id,
        workload_id=workload.id,
        content={},
        digest="b" * 64,
        target_epoch="epoch-active",
        rationale="test evidence",
        created_by=actor_id,
        expires_at=now + timedelta(hours=1),
    )
    newer_plan = ActionPlan(
        incident_id=incident.id,
        workload_id=workload.id,
        content={},
        digest="c" * 64,
        target_epoch="epoch-newer",
        rationale="test evidence",
        created_by=actor_id,
        expires_at=now + timedelta(hours=1),
    )
    db.add_all([unknown_plan, active_plan, newer_plan])
    db.flush()
    unknown = Execution(
        plan_id=unknown_plan.id,
        workload_id=workload.id,
        state="outcome_unknown",
        holds_lock=True,
        maintenance_released=False,
    )
    active = Execution(
        plan_id=active_plan.id,
        workload_id=workload.id,
        state="verifying",
        holds_lock=False,
        maintenance_released=False,
        created_at=now,
    )
    newer = Execution(
        plan_id=newer_plan.id,
        workload_id=workload.id,
        state="succeeded",
        holds_lock=False,
        maintenance_released=False,
        created_at=now + timedelta(seconds=1),
    )
    db.add_all([unknown, active, newer])
    db.commit()

    resolved = list_incidents(db, 25, 0, view="all")
    blocked = list_incidents(db, 25, 0, view="blocked")

    assert [item.id for item in resolved.items] == [incident.id]
    assert {item.id for item in resolved.items[0].relevant_executions} == {unknown.id, active.id}
    assert {item.classification for item in resolved.items[0].relevant_executions} == {
        "terminal_unknown_locked",
        "active",
    }
    assert blocked.items == []


def test_creation_anchor_excludes_incidents_created_after_first_page(db, actor_id):
    workload = create_workload(db, "queue-anchor")
    initial = create_incident(db, workload.id, actor_id, title="Initial")
    db.commit()

    first = list_incidents(db, 25, 0)
    later = create_incident(
        db,
        workload.id,
        actor_id,
        title="Created later",
        created_at=first.creation_anchor.created_at + timedelta(seconds=1),
    )
    db.commit()

    anchored = list_incidents(
        db,
        25,
        0,
        anchor_created_at=first.creation_anchor.created_at,
        anchor_id=first.creation_anchor.id,
    )
    refreshed = list_incidents(db, 25, 0)

    assert [item.id for item in anchored.items] == [initial.id]
    assert later.id in {item.id for item in refreshed.items}


def test_queue_projection_includes_requester_and_acknowledgment_actor_time(db, actor_id):
    workload = create_workload(db, "queue-actors")
    incident = create_incident(
        db, workload.id, actor_id, title="Attributed incident", state="acknowledged"
    )
    activity = IncidentActivity(
        incident_id=incident.id,
        kind="acknowledge",
        text="Investigating",
        actor_id=actor_id,
    )
    db.add(activity)
    db.commit()

    result = list_incidents(db, 25, 0)
    item = next(row for row in result.items if row.id == incident.id)

    assert item.created_by_name == "queue-operator"
    assert item.acknowledged_by == actor_id
    assert item.acknowledged_by_name == "queue-operator"
    assert item.acknowledged_at == activity.created_at
