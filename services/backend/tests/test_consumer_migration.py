from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from cloudops.shared.database import BACKEND_ROOT, engine


def test_consumer_upgrade_preserves_populated_foundation_and_has_one_head():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0002_foundation")
    user, workload, incident = uuid4(), uuid4(), uuid4()
    with engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO users (id,username,password_hash,role) VALUES (:id,'prior-user','prior-hash','viewer')"
            ),
            {"id": user},
        )
        db.execute(
            text(
                "INSERT INTO workloads (id,cluster,namespace,name,uid,active) VALUES (:id,'prior-cluster','demo','prior','prior-uid',true)"
            ),
            {"id": workload},
        )
        db.execute(
            text(
                "INSERT INTO incidents (id,workload_id,title,description,state,created_by) VALUES (:id,:workload,'Prior incident','Preserve prior authored input','open',:actor)"
            ),
            {"id": incident, "workload": workload, "actor": user},
        )
    for _ in range(2):
        command.upgrade(config, "head")
    with engine().connect() as db:
        assert db.execute(
            text("SELECT password_hash,role FROM users WHERE id=:id"), {"id": user}
        ).one() == ("prior-hash", "viewer")
        assert db.execute(
            text("SELECT uid,source FROM workloads WHERE id=:id"), {"id": workload}
        ).one() == ("prior-uid", "development_fixture")
        assert db.execute(
            text("SELECT workload_id,description,state FROM incidents WHERE id=:id"),
            {"id": incident},
        ).one() == (workload, "Preserve prior authored input", "open")
    assert (
        next(c for c in inspect(engine()).get_columns("workloads") if c["name"] == "source")[
            "default"
        ]
        is None
    )
    assert ScriptDirectory.from_config(config).get_heads() == ["0006_incident_requests"]
    command.check(config)


def test_observation_upgrade_preserves_existing_baselines_sessions_and_incidents(client):
    from conftest import sign_in
    from cloudops.features.workloads.models import Baseline, BaselineCandidate
    from cloudops.shared.database import sessions

    headers = sign_in(client)
    workload = client.get("/api/workloads").json()["items"][0]
    response = client.post(
        "/api/incidents",
        json={"workload_id": workload["id"], "title": "Migration preservation"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    with sessions()() as db, db.begin():
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        candidate = BaselineCandidate(
            workload_id=__import__("uuid").UUID(workload["id"]),
            digest="f" * 64,
            snapshot={"source": "migration_fixture", "quantity": "100m"},
        )
        db.add(candidate)
        db.flush()
        db.add(
            Baseline(
                workload_id=candidate.workload_id,
                candidate_id=candidate.id,
                digest=candidate.digest,
                snapshot=candidate.snapshot,
                confirmed_by=actor,
            )
        )

    # 0006 request metadata is outside this receipt; its own migration test covers it.
    added = (
        "origin_kind,origin_source_id,first_seen_at,last_seen_at,severity,severity_provenance,"
        "alert_signal,question,question_source,window_start,window_end,window_source,"
        "queue_reason,outcome_code"
    )

    def records():
        with engine().connect() as connection:
            result = {
                table: list(
                    connection.execute(
                        text(
                            f"SELECT (row_to_json(t)::jsonb - string_to_array('{added}', ','))::text "
                            f"FROM {table} t ORDER BY 1"
                        )
                    )
                )
                for table in (
                    "users",
                    "sessions",
                    "incidents",
                    "investigation_runs",
                    "baselines",
                    "baseline_candidates",
                )
            }
            result["workloads"] = list(
                connection.execute(
                    text(
                        "SELECT id,cluster,namespace,name,uid,active,source FROM workloads ORDER BY id"
                    )
                )
            )
            return result

    before = records()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0003_e01_consumers")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert records() == before
    command.check(config)
