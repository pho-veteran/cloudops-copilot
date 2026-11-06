"""Actual PostgreSQL/API reads over explicitly synthetic persisted evidence."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from conftest import sign_in
from sqlalchemy import select
from test_access_admission import create, incident_body, rerun

from cloudops.entrypoints.worker import run_once
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import Evidence, EvidencePin, InvestigationRun
from cloudops.features.investigations.locators import digest
from cloudops.features.workloads.models import Workload
from cloudops.shared.config import settings
from cloudops.shared.database import sessions


@pytest.fixture
def pair(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    left = create(client, headers, incident_body(client)).json()
    assert run_once()
    right = rerun(client, headers, left["incident_id"]).json()
    assert run_once()
    return left, right


def endpoint(ids):
    return f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}"


def compare(client, pair, **query):
    left, right = pair
    return client.get(
        endpoint(left) + "/compare",
        params={
            "right_incident_id": right["incident_id"],
            "right_run_id": right["run_id"],
            **query,
        },
    )


def fixture(client, ids):
    return next(
        item
        for item in client.get(endpoint(ids)).json()["evidence"]
        if item["source_key"] == "development_fixture"
    )


def test_compare_and_citation_reads_preserve_hashes_and_pins(client, pair):
    record = fixture(client, pair[0])
    with sessions()() as db:
        original_pins = list(
            db.execute(select(EvidencePin.evidence_id, EvidencePin.kind, EvidencePin.owner_id))
        )
    response = compare(client, pair)
    assert response.status_code == 200, response.text
    assert len(response.json()["sources"]) == 12
    detail = compare(client, pair, source_key="development_fixture", catalog_version="standard_v1")
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"]
    base = endpoint(pair[0]) + f"/evidence/{record['id']}"
    targets = client.get(base + "/citation-targets").json()
    target = targets["items"][0]["locator"]
    result = client.get(
        base + "/citation", params={"locator_kind": target["kind"], "locator_key": target["key"]}
    )
    assert result.status_code == 200 and result.json()["target"]["locator"] == target
    assert fixture(client, pair[0])["sha256"] == record["sha256"]
    with sessions()() as db:
        assert (
            list(
                db.execute(select(EvidencePin.evidence_id, EvidencePin.kind, EvidencePin.owner_id))
            )
            == original_pins
        )


def test_paging_is_exact_bounded_and_bound_to_selection(client, pair):
    params = {"source_key": "development_fixture", "catalog_version": "standard_v1", "limit": 1}
    all_rows = compare(client, pair, **{**params, "limit": 100}).json()["items"]
    actual, cursor = [], None
    while True:
        response = compare(client, pair, **params, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        result = response.json()
        actual.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert compare(client, pair, **params, cursor=cursor, changed_only=True).status_code == 409
    assert actual == all_rows
    assert compare(client, pair, **params, cursor="garbage").status_code == 422


def test_citation_does_not_fallback_to_another_run_or_target(client, pair):
    record = fixture(client, pair[0])
    wrong = endpoint(pair[1]) + f"/evidence/{record['id']}/citation"
    assert client.get(wrong).status_code == 404
    exact = endpoint(pair[0]) + f"/evidence/{record['id']}/citation"
    assert (
        client.get(
            exact, params={"locator_kind": "event", "locator_key": "v1_" + "0" * 64}
        ).status_code
        == 404
    )
    assert client.get(exact, params={"locator_kind": "event"}).status_code == 422
    assert (
        client.get(exact, params={"locator_kind": "event", "locator_key": "../../file"}).status_code
        == 422
    )


def test_viewer_can_read_both_sides_but_anonymous_cannot(client, pair):
    sign_in(client, "viewer")
    assert compare(client, pair).status_code == 200
    client.cookies.clear()
    assert compare(client, pair).status_code == 401


def test_wrong_right_incident_is_not_disclosed(client, pair):
    response = compare(client, pair, right_incident_id=str(uuid4()))
    assert response.status_code == 404
    assert pair[0]["run_id"] not in response.text


def test_replacement_and_unrelated_identities_do_not_align_values(client, pair):
    left, right = pair
    with sessions()() as db, db.begin():
        incident = db.get(Incident, UUID(right["incident_id"]))
        old = db.get(Workload, incident.workload_id)
        replacement = Workload(
            cluster=old.cluster,
            namespace=old.namespace,
            name=old.name,
            uid=str(uuid4()),
            source=old.source,
        )
        db.add(replacement)
        db.flush()
        # Split the second Run to another incident to preserve the old history.
        separate = Incident(
            workload_id=replacement.id,
            title="Replacement contract fixture",
            description="Persisted fixture",
            created_by=incident.created_by,
        )
        db.add(separate)
        db.flush()
        db.get(InvestigationRun, UUID(right["run_id"])).incident_id = separate.id
        right = {**right, "incident_id": str(separate.id)}
    result = compare(client, (left, right))
    assert result.status_code == 200, result.text
    assert result.json()["identity_relation"] == "replacement"
    assert result.json()["status"] == "not_comparable"
    assert result.json()["items"] == []
    with sessions()() as db, db.begin():
        db.get(Workload, replacement.id).name = "unrelated"
    assert compare(client, (left, right)).status_code == 422


def test_expired_history_keeps_metadata_and_never_becomes_empty_success(client, pair):
    record = fixture(client, pair[0])
    with sessions()() as db, db.begin():
        db.get(Evidence, UUID(record["id"])).expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        for pin in db.scalars(
            select(EvidencePin).where(EvidencePin.evidence_id == UUID(record["id"]))
        ):
            db.delete(pin)
    source = next(
        row
        for row in compare(client, pair).json()["sources"]
        if row["source_key"] == "development_fixture"
    )
    assert source["status"] == "unavailable" and source["left"]["sha256"] == record["sha256"]
    base = endpoint(pair[0]) + f"/evidence/{record['id']}"
    assert client.get(base + "/citation").status_code == 200
    assert client.get(base + "/citation-targets").status_code == 410


def test_changed_fields_and_swap_preserve_stable_refs(client, pair):
    # This is a stored state contract fixture, not a collector observation.
    for ids, replicas in zip(pair, (1, 2), strict=True):
        with sessions()() as db, db.begin():
            row = db.scalar(
                select(Evidence).where(
                    Evidence.run_id == UUID(ids["run_id"]),
                    Evidence.source_key == "kubernetes.state",
                )
            )
            row.payload = {"deployment": {"uid": "fixture-deployment", "replicas": replicas}}
            row.sha256 = digest(row.payload)
            row.coverage = {"status": "available", "reason_codes": []}
    params = {
        "source_key": "kubernetes.state",
        "catalog_version": "standard_v1",
        "changed_only": True,
    }
    forward = compare(client, pair, **params).json()["items"]
    backward = compare(client, pair[::-1], **params).json()["items"]
    assert len(forward) == len(backward) == 1
    assert forward[0]["left"] == backward[0]["right"] == 1
    assert forward[0]["right"] == backward[0]["left"] == 2
    assert forward[0]["key"] == backward[0]["key"]


@pytest.mark.parametrize("state", ["queued", "collecting", "failed", "cancelled"])
def test_non_successful_runs_remain_explicit_without_hiding_the_left_history(client, pair, state):
    with sessions()() as db, db.begin():
        row = db.get(InvestigationRun, UUID(pair[1]["run_id"]))
        row.state = state
        for evidence in db.scalars(select(Evidence).where(Evidence.run_id == row.id)):
            for pin in db.scalars(
                select(EvidencePin).where(EvidencePin.evidence_id == evidence.id)
            ):
                db.delete(pin)
            db.flush()
            db.delete(evidence)
    result = compare(client, pair)
    assert result.status_code == 200, result.text
    assert result.json()["right"]["state"] == state
    assert all(source["right"] is None for source in result.json()["sources"])
    assert all(source["status"] == "unavailable" for source in result.json()["sources"])
    assert fixture(client, pair[0])["content_status"] == "available"


def test_cross_incident_same_identity_is_readable_and_right_authorization_runs(
    client, pair, monkeypatch
):
    from cloudops.features.investigations import comparison_service
    from cloudops.shared.errors import DomainError

    with sessions()() as db, db.begin():
        original = db.get(Incident, UUID(pair[0]["incident_id"]))
        another = Incident(
            workload_id=original.workload_id,
            title="Cross-incident contract fixture",
            description="Fixture",
            created_by=original.created_by,
        )
        db.add(another)
        db.flush()
        db.get(InvestigationRun, UUID(pair[1]["run_id"])).incident_id = another.id
        right = {**pair[1], "incident_id": str(another.id)}
    assert compare(client, (pair[0], right)).status_code == 200
    previous = comparison_service.get_incident

    def authorized(db, incident_id):
        if incident_id == another.id:
            raise DomainError(404, "incident_not_found", "Incident not found.")
        return previous(db, incident_id)

    monkeypatch.setattr(comparison_service, "get_incident", authorized)
    response = compare(client, (pair[0], right))
    assert response.status_code == 404
    assert pair[0]["run_id"] not in response.text and right["run_id"] not in response.text


def test_comparison_keeps_saved_handoff_bytes_and_never_calls_a_provider(client, pair, monkeypatch):
    from cloudops.features.investigations import collector
    from cloudops.features.incidents import handoff

    headers = sign_in(client)
    incident = pair[0]["incident_id"]
    resolved = client.post(
        f"/api/incidents/{incident}/lifecycle",
        headers=headers,
        json={"action": "resolve", "reason": "Persisted comparison fixture"},
    )
    assert resolved.status_code == 200
    activity = client.get(f"/api/incidents/{incident}/activity").json()["items"]
    resolution = next(item["id"] for item in activity if item["kind"] == "resolve")
    export = f"/api/incidents/{incident}/handoff?resolution_id={resolution}"
    original_bytes = client.get(export).content

    def forbidden(*args, **kwargs):
        raise AssertionError("A read must not collect, compose new saved data or call a provider.")

    monkeypatch.setattr(collector, "configured_adapters", forbidden)
    monkeypatch.setattr(handoff, "compose", forbidden)
    assert compare(client, pair).status_code == 200
    meta = fixture(client, pair[0])
    assert (
        client.get(endpoint(pair[0]) + f"/evidence/{meta['id']}/citation-targets").status_code
        == 200
    )
    assert client.get(export).content == original_bytes


def test_legacy_and_incompatible_schema_keep_original_metadata(client, pair):
    record = fixture(client, pair[0])
    with sessions()() as db, db.begin():
        row = db.get(Evidence, UUID(record["id"]))
        row.schema_version = 1
        row.catalog_version = "legacy_v1"
    sources = compare(client, pair).json()["sources"]
    legacy = next(source for source in sources if source["catalog_version"] == "legacy_v1")
    assert legacy["status"] == "unavailable" and legacy["right"] is None
    assert legacy["left"]["sha256"] == record["sha256"]
    with sessions()() as db, db.begin():
        row = db.get(Evidence, UUID(record["id"]))
        row.catalog_version = "standard_v1"
    sources = compare(client, pair).json()["sources"]
    assert (
        next(source for source in sources if source["source_key"] == "development_fixture")[
            "status"
        ]
        == "not_comparable"
    )


def test_ambiguous_event_target_fails_closed(client, pair):
    with sessions()() as db, db.begin():
        row = db.scalar(
            select(Evidence).where(
                Evidence.run_id == UUID(pair[0]["run_id"]),
                Evidence.source_key == "kubernetes.events",
            )
        )
        row.payload = {
            "events": [{"uid": "duplicate", "count": 1}, {"uid": "duplicate", "count": 2}]
        }
        row.sha256 = digest(row.payload)
        row.coverage = {"status": "available", "reason_codes": []}
        eid = str(row.id)
    base = endpoint(pair[0]) + f"/evidence/{eid}"
    target = client.get(base + "/citation-targets").json()["items"][0]
    assert target["ambiguous"]
    assert (
        client.get(
            base + "/citation",
            params={
                "locator_kind": target["locator"]["kind"],
                "locator_key": target["locator"]["key"],
            },
        ).status_code
        == 409
    )


def test_status_filter_is_typed_and_applies_to_semantic_rows(client, pair):
    params = {"source_key": "development_fixture", "catalog_version": "standard_v1"}
    for status in ("added", "removed", "changed", "unchanged", "unavailable", "not_comparable"):
        response = compare(client, pair, **params, difference_status=status)
        assert response.status_code == 200, response.text
        assert all(row["status"] == status for row in response.json()["items"])
        if status == "unchanged":
            assert response.json()["items"]
    assert compare(client, pair, **params, difference_status="unknown").status_code == 422


def test_summary_never_loads_retained_payloads(client, pair, monkeypatch):
    import re
    from sqlalchemy import event
    from cloudops.shared.database import engine
    from cloudops.features.investigations import comparison_service

    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata comparison must not invoke a payload reader.")

    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine(), "before_cursor_execute", capture)
    monkeypatch.setattr(comparison_service, "evidence_content", forbidden)
    try:
        assert compare(client, pair).status_code == 200
    finally:
        event.remove(engine(), "before_cursor_execute", capture)
    assert statements
    for statement in statements:
        selected_columns = statement.lower().split("from", 1)[0]
        assert not re.search(r"\bevidence\.payload(?:\s|,|$)", selected_columns)


def test_repeatable_read_compare_survives_a_real_payload_purge_boundary(client, pair, monkeypatch):
    from cloudops.features.investigations import comparison_service, retention

    record = fixture(client, pair[0])
    now = datetime.now(timezone.utc)
    with sessions()() as db, db.begin():
        db.get(Evidence, UUID(record["id"])).expires_at = now + timedelta(days=1)
    # Controlled maintenance clock, actual purge transaction on another DB
    # connection while the API's read-only repeatable-read snapshot is open.
    monkeypatch.setattr(retention, "database_now", lambda db: now + timedelta(days=2))
    original = comparison_service.evidence_content
    purged = []

    def content_after_purge(*args, **kwargs):
        if not purged:
            with sessions()() as db:
                purged.append(retention.purge_expired(db, apply=True))
        return original(*args, **kwargs)

    monkeypatch.setattr(comparison_service, "evidence_content", content_after_purge)
    response = compare(
        client, pair, source_key="development_fixture", catalog_version="standard_v1"
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] and purged == [1]
    assert response.json()["sources"][0]["left"]["sha256"] == record["sha256"]
    later = compare(client, pair).json()["sources"][0]
    assert later["left"]["content_status"] == "expired"
    assert later["left"]["sha256"] == record["sha256"]
