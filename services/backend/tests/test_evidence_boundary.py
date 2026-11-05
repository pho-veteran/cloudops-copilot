"""Evidence content is scoped, immutable and independent of its retention metadata."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from conftest import sign_in
from sqlalchemy import delete, select, update
from test_access_admission import create, incident_body

from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import SOURCES
from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.features.investigations.retention import purge_expired
from cloudops.features.investigations.schemas import Coverage, SourceResult, SourceTimes
from cloudops.features.investigations.storage import store_batch
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import sessions


def batch(now):
    results = []
    for key, spec in SOURCES.items():
        payload = None
        coverage = Coverage(status="unavailable", reason_codes=["source_not_configured"])
        if key == "development_fixture":
            coverage = Coverage(status="available", reason_codes=[])
            payload = {
                "label": "Synthetic development evidence; no live cluster was contacted.",
                "workload": "cluster/ns/app",
                "observation": "Live workload health and telemetry are unavailable.",
            }
        results.append(
            SourceResult(
                source_key=key,
                payload_kind=spec.payload_kind,
                payload_version=1,
                coverage=coverage,
                source_times=SourceTimes(
                    observed_at=now, collected_at=now, time_relation="not_captured"
                ),
                redacted_bounded_payload=payload,
            )
        )
    return results


def setup_run(client):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    run_id, incident_id = UUID(created["run_id"]), UUID(created["incident_id"])
    with sessions()() as db:
        incident = db.get(Incident, incident_id)
        uid = db.get(Workload, incident.workload_id).uid
    return incident_id, run_id, uid


def test_publication_is_immutable_and_metadata_has_no_content(client):
    incident_id, run_id, uid = setup_run(client)
    source_results = batch(datetime.now(timezone.utc))
    with sessions()() as db:
        rows = store_batch(db, run_id, uid, source_results)
        ids = {r.source_key: r.id for r in rows}
        db.commit()
    with sessions()() as db:
        assert {r.source_key: r.id for r in store_batch(db, run_id, uid, source_results)} == ids
        db.commit()
    changed = list(source_results)
    index = next(i for i, r in enumerate(changed) if r.source_key == "development_fixture")
    payload = dict(changed[index].redacted_bounded_payload, workload="different")
    changed[index] = changed[index].model_copy(update={"redacted_bounded_payload": payload})
    with sessions()() as db, pytest.raises(ValueError):
        store_batch(db, run_id, uid, changed)
    sign_in(client, "viewer")
    base = f"/api/incidents/{incident_id}/runs/{run_id}"
    data = client.get(base).json()
    assert len(data["evidence"]) == len(SOURCES)
    assert all("payload" not in item for item in data["evidence"])
    evidence_id = ids["development_fixture"]
    content = client.get(f"{base}/evidence/{evidence_id}/content")
    assert content.status_code == 200
    assert content.json()["payload"]["workload"] == "cluster/ns/app"
    assert (
        client.get(f"/api/incidents/{uuid4()}/runs/{run_id}/evidence/{evidence_id}").status_code
        == 404
    )
    assert client.get(f"{base}/evidence/{uuid4()}/content").status_code == 404
    assert client.post("/api/evidence-purge").status_code in (404, 405)


def test_expiry_preserves_digest_and_respects_pins(client):
    incident_id, run_id, uid = setup_run(client)
    results = batch(datetime.now(timezone.utc))
    with sessions()() as db:
        row = next(
            r
            for r in store_batch(db, run_id, uid, results)
            if r.source_key == "development_fixture"
        )
        evidence_id, digest = row.id, row.sha256
        db.commit()
    with sessions()() as db:
        db.execute(
            update(Evidence)
            .where(Evidence.id == evidence_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()
    with sessions()() as db:
        assert purge_expired(db) == 0  # the latest useful collection remains pinned
    with sessions()() as db:
        db.execute(delete(EvidencePin).where(EvidencePin.evidence_id == evidence_id))
        db.commit()
    with sessions()() as db:
        assert purge_expired(db) == 1
    with sessions()() as db:
        assert purge_expired(db, apply=True) == 1
    with sessions()() as db:
        assert (
            db.scalar(
                select(Evidence.id).where(Evidence.id == evidence_id, Evidence.payload.is_(None))
            )
            == evidence_id
        )
        assert purge_expired(db, apply=True) == 0
    base = f"/api/incidents/{incident_id}/runs/{run_id}/evidence/{evidence_id}"
    metadata = client.get(base).json()
    assert metadata["sha256"] == digest and metadata["content_status"] == "expired"
    response = client.get(base + "/content")
    assert response.status_code == 410 and response.json()["error"]["code"] == "evidence_expired"


def test_existing_foreign_run_and_corrupted_uid_are_not_readable_through_another_scope(
    client, monkeypatch
):
    from cloudops.entrypoints.worker import run_once
    from cloudops.shared.config import settings

    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()
    assert run_once()
    with sessions()() as db, db.begin():
        foreign = Workload(
            cluster="development",
            namespace="other-fixture",
            name="foreign",
            uid="other-original-uid",
            source="development_fixture",
            active=True,
        )
        db.add(foreign)
        db.flush()
        foreign_id = str(foreign.id)
    second_response = create(
        client,
        headers,
        {
            "workload_id": foreign_id,
            "title": "Different target fixture",
        },
    )
    assert second_response.status_code == 201
    second = second_response.json()
    assert run_once()
    first_run = client.get(f"/api/incidents/{first['incident_id']}/runs/{first['run_id']}").json()
    second_run = client.get(
        f"/api/incidents/{second['incident_id']}/runs/{second['run_id']}"
    ).json()
    first_id = next(
        row["id"] for row in first_run["evidence"] if row["source_key"] == "development_fixture"
    )
    second_id = next(
        row["id"] for row in second_run["evidence"] if row["source_key"] == "development_fixture"
    )
    sign_in(client, "viewer")
    for path in (
        f"/api/incidents/{first['incident_id']}/runs/{first['run_id']}/evidence/{second_id}",
        f"/api/incidents/{first['incident_id']}/runs/{second['run_id']}/evidence/{second_id}",
    ):
        for suffix in ("", "/content"):
            response = client.get(path + suffix)
            assert response.status_code == 404
            assert "other-original-uid" not in response.text
    # This deliberately corrupt fixture verifies the reader's UID fence separately
    # from publication's rejection of a wrong CollectionResult UID.
    with sessions()() as db, db.begin():
        db.execute(
            update(Evidence)
            .where(Evidence.id == UUID(first_id))
            .values(workload_uid="other-original-uid")
        )
    path = f"/api/incidents/{first['incident_id']}/runs/{first['run_id']}/evidence/{first_id}"
    assert client.get(path).status_code == client.get(path + "/content").status_code == 404
