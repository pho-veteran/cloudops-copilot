"""API/worker history survives failures, resolution, reopening and retention."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from conftest import sign_in
from sqlalchemy import select
from test_access_admission import create, incident_body, rerun
from test_jobs import collect_claim

from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.investigations import retention
from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.features.investigations.storage import store_batch
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.jobs import claim_next, finish


def read_run(client, ids):
    response = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def fixture_record(run):
    return next(row for row in run["evidence"] if row["source_key"] == "development_fixture")


def latest_pins(incident_id):
    with sessions()() as db:
        return set(
            db.scalars(
                select(EvidencePin.evidence_id).where(
                    EvidencePin.kind == "latest", EvidencePin.owner_id == UUID(incident_id)
                )
            )
        )


def transition(client, headers, incident_id, action):
    response = client.post(
        f"/api/incidents/{incident_id}/lifecycle",
        headers=headers,
        json={"action": action, "reason": "Evidence lifecycle regression fixture"},
    )
    assert response.status_code == 200, response.text


def test_failed_and_all_unavailable_successors_keep_latest_useful_until_replacement(
    client, monkeypatch
):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()
    assert run_once()
    first_record = fixture_record(read_run(client, first))
    original_pin = {UUID(first_record["id"])}
    assert latest_pins(first["incident_id"]) == original_pin

    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "none")
    unavailable = rerun(client, headers, first["incident_id"]).json()
    assert run_once()
    value = read_run(client, unavailable)
    assert value["state"] == "insufficient_evidence"
    assert value["outcome_code"] == "analysis_unavailable"
    assert all(row["content_status"] == "unavailable" for row in value["evidence"])
    assert latest_pins(first["incident_id"]) == original_pin

    monkeypatch.setenv("COLLECTION_SOURCE_MODE", "development_fixture")
    monkeypatch.setenv("COLLECTOR_FAIL", "true")
    settings.cache_clear()
    failed = rerun(client, headers, first["incident_id"]).json()
    for _ in range(3):
        assert run_once()
    value = read_run(client, failed)
    assert (value["state"], value["outcome_code"]) == ("failed", "collection_failed")
    assert value["evidence"] == []
    assert latest_pins(first["incident_id"]) == original_pin

    # A later useful Run replaces only latest-useful pins. A retained finding is
    # a labeled persistence contract fixture, not a production E06 producer.
    finding_owner = uuid4()
    with sessions()() as db, db.begin():
        db.scalar(select(Evidence).where(Evidence.id == UUID(first_record["id"])).with_for_update())
        db.add(
            EvidencePin(
                evidence_id=UUID(first_record["id"]), kind="finding", owner_id=finding_owner
            )
        )
    monkeypatch.setenv("COLLECTOR_FAIL", "false")
    settings.cache_clear()
    replacement = rerun(client, headers, first["incident_id"]).json()
    assert run_once()
    replacement_record = fixture_record(read_run(client, replacement))
    assert latest_pins(first["incident_id"]) == {UUID(replacement_record["id"])}
    monkeypatch.setattr(
        retention, "database_now", lambda db: datetime.now(timezone.utc) + timedelta(days=31)
    )
    with sessions()() as db:
        assert retention.purge_expired(db, apply=True) == 0
        assert db.get(EvidencePin, (UUID(first_record["id"]), "finding", finding_owner)) is not None
    assert fixture_record(read_run(client, first))["sha256"] == first_record["sha256"]


def test_saved_reference_bytes_and_pins_survive_reopen_and_new_useful_run(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()
    assert run_once()
    original = fixture_record(read_run(client, first))
    transition(client, headers, first["incident_id"], "resolve")
    activity = client.get(f"/api/incidents/{first['incident_id']}/activity").json()
    resolution_id = next(row["id"] for row in activity["items"] if row["kind"] == "resolve")
    path = f"/api/incidents/{first['incident_id']}/handoff"
    params = {"resolution_id": resolution_id}
    saved = client.get(path, params=params).content
    snapshot = client.get(path + "/preview", params=params).json()["snapshot"]
    saved_ref = next(row for row in snapshot["evidence"]["items"] if row["id"] == original["id"])
    assert saved_ref["pinned"] and saved_ref["payload_ref"] == original["id"]
    assert all("payload" not in row for row in snapshot["evidence"]["items"])

    transition(client, headers, first["incident_id"], "reopen")
    assert latest_pins(first["incident_id"]) == {UUID(original["id"])}
    replacement = rerun(client, headers, first["incident_id"]).json()
    assert run_once()
    replacement_ref = fixture_record(read_run(client, replacement))
    assert latest_pins(first["incident_id"]) == {UUID(replacement_ref["id"])}
    monkeypatch.setattr(
        retention, "database_now", lambda db: datetime.now(timezone.utc) + timedelta(days=31)
    )
    with sessions()() as db:
        assert retention.purge_expired(db, apply=True) == 0
        assert (
            db.get(EvidencePin, (UUID(original["id"]), "resolution", UUID(resolution_id)))
            is not None
        )
    assert client.get(path, params=params).content == saved
    assert client.get(path).content != saved
    assert fixture_record(read_run(client, first))["content_status"] == "available"


def test_reopen_pins_claimed_collection_that_finished_after_resolution(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    claim = claim_next(HANDLERS)
    result = collect_claim(claim)
    transition(client, headers, ids["incident_id"], "resolve")
    assert finish(claim, HANDLERS[claim.kind], result=result)
    assert latest_pins(ids["incident_id"]) == set()
    record = fixture_record(read_run(client, ids))
    transition(client, headers, ids["incident_id"], "reopen")
    assert latest_pins(ids["incident_id"]) == {UUID(record["id"])}


def test_duplicate_old_publication_cannot_rewind_latest_useful_pin(client, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()
    claim = claim_next(HANDLERS)
    original_result = collect_claim(claim)
    assert finish(claim, HANDLERS[claim.kind], result=original_result)
    replacement = rerun(client, headers, first["incident_id"]).json()
    assert run_once()
    replacement_ref = fixture_record(read_run(client, replacement))
    with sessions()() as db, db.begin():
        store_batch(
            db, UUID(first["run_id"]), original_result.workload_uid, original_result.source_results
        )
    assert latest_pins(first["incident_id"]) == {UUID(replacement_ref["id"])}
