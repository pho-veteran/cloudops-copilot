"""A populated pre-source database retains evidence and frozen resolution bytes."""

import json
from uuid import uuid4

from alembic import command
from alembic.config import Config
from conftest import sign_in
from sqlalchemy import text

from cloudops.shared.database import BACKEND_ROOT, engine


def test_legacy_evidence_and_resolution_survive_repeated_upgrade(client):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0007_alert_intake")
    user, workload, incident, run, evidence, activity = [uuid4() for _ in range(6)]
    payload = {
        "label": "Synthetic development evidence; no live cluster was contacted.",
        "workload": "cluster/ns/demo",
        "observation": "Live workload health and telemetry are unavailable.",
    }
    snapshot = {
        "schema_version": "HandoffSnapshot/v1",
        "snapshot": {"before": "migration"},
        "markdown": "Exact saved report\n\nOriginal bytes.\n",
    }
    with engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO users (id,username,password_hash,role) VALUES (:id,'old','hash','operator')"
            ),
            {"id": user},
        )
        db.execute(
            text(
                "INSERT INTO workloads (id,cluster,namespace,name,uid,active,source) VALUES (:id,'c','n','w','old-uid',true,'development_fixture')"
            ),
            {"id": workload},
        )
        db.execute(
            text(
                "INSERT INTO incidents (id,workload_id,title,description,state,created_by) VALUES (:id,:w,'old','','resolved',:u)"
            ),
            {"id": incident, "w": workload, "u": user},
        )
        db.execute(
            text(
                "INSERT INTO investigation_runs (id,incident_id,state,reason,requested_by) VALUES (:id,:i,'insufficient_evidence','old',:u)"
            ),
            {"id": run, "i": incident, "u": user},
        )
        db.execute(
            text(
                "INSERT INTO evidence (id,run_id,workload_uid,source,schema_version,observed_at,collected_at,sha256,payload) VALUES (:id,:r,'old-uid','development_fixture',1,now(),now(),:sha,CAST(:p AS jsonb))"
            ),
            {"id": evidence, "r": run, "sha": "a" * 64, "p": json.dumps(payload)},
        )
        db.execute(
            text(
                "INSERT INTO incident_activity (id,incident_id,kind,text,actor_id,snapshot) VALUES (:id,:i,'resolve','done',:u,CAST(:s AS jsonb))"
            ),
            {"id": activity, "i": incident, "u": user, "s": json.dumps(snapshot)},
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine().connect() as db:
        row = db.execute(
            text(
                "SELECT id,sha256,payload,source_key,catalog_version,requested_window,time_relation FROM evidence WHERE id=:id"
            ),
            {"id": evidence},
        ).one()
        assert tuple(row) == (
            evidence,
            "a" * 64,
            payload,
            "development_fixture",
            "legacy_v1",
            None,
            "not_captured",
        )
        assert (
            db.scalar(text("SELECT snapshot FROM incident_activity WHERE id=:id"), {"id": activity})
            == snapshot
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM evidence_pins WHERE evidence_id=:id AND kind='resolution'"
                ),
                {"id": evidence},
            )
            == 1
        )
    sign_in(client, "viewer")
    path = f"/api/incidents/{incident}/runs/{run}/evidence/{evidence}"
    metadata = client.get(path)
    assert metadata.status_code == 200
    assert metadata.json()["schema_version"] == 1
    assert metadata.json()["coverage"] == {
        "status": "unavailable",
        "reason_codes": ["not_captured"],
    }
    assert metadata.json()["content_status"] == "available"
    content = client.get(path + "/content")
    assert content.status_code == 200
    assert content.json()["payload"] == payload and content.json()["sha256"] == "a" * 64
    saved = client.get(
        f"/api/incidents/{incident}/handoff", params={"resolution_id": str(activity)}
    )
    assert saved.status_code == 200 and saved.content == snapshot["markdown"].encode()
