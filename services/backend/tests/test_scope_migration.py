"""Populated upgrade receipt for identities, sessions and immutable baseline JSON."""

from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from cloudops.shared.database import BACKEND_ROOT, engine


def test_populated_scope_upgrade_preserves_existing_records_and_baseline_bytes():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0003_e01_consumers")
    actor, workload, candidate, baseline, incident, run = [uuid4() for _ in range(6)]
    with engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO users (id,username,password_hash,role) VALUES (:id,'scope-prior','hash-preserve','operator')"
            ),
            {"id": actor},
        )
        db.execute(
            text(
                "INSERT INTO workloads (id,cluster,namespace,name,uid,active,source) VALUES (:id,'lab','demo','prior','prior-uid',true,'live_kubernetes')"
            ),
            {"id": workload},
        )
        # Take column names from the real schema rather than inferring session internals.
        db.execute(
            text(
                "INSERT INTO sessions (user_id,token_hash,csrf_token,expires_at) VALUES (:actor,'prior-token-hash','prior-csrf',now()+interval '1 hour')"
            ),
            {"actor": actor},
        )
        db.execute(
            text(
                "INSERT INTO baseline_candidates (id,workload_id,digest,snapshot) VALUES (:id,:workload,:digest,CAST(:snapshot AS jsonb))"
            ),
            {
                "id": candidate,
                "workload": workload,
                "digest": "d" * 64,
                "snapshot": '{"original":"Giữ nguyên","containers":[]} ',
            },
        )
        db.execute(
            text(
                "INSERT INTO baselines (id,workload_id,candidate_id,digest,snapshot,confirmed_by) SELECT :id,workload_id,id,digest,snapshot,:actor FROM baseline_candidates WHERE id=:candidate"
            ),
            {"id": baseline, "actor": actor, "candidate": candidate},
        )
        db.execute(
            text(
                "INSERT INTO incidents (id,workload_id,title,description,state,created_by) VALUES (:id,:workload,'Prior incident','Original input','open',:actor)"
            ),
            {"id": incident, "workload": workload, "actor": actor},
        )
        db.execute(
            text(
                "INSERT INTO investigation_runs (id,incident_id,requested_by,reason,state) VALUES (:id,:incident,:actor,'Preserve prior reason','queued')"
            ),
            {"id": run, "incident": incident, "actor": actor},
        )
        before = db.execute(
            text(
                "SELECT snapshot::text,digest,confirmed_by,confirmed_at FROM baselines WHERE id=:id"
            ),
            {"id": baseline},
        ).one()
    for _ in range(2):
        command.upgrade(config, "head")
    with engine().connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT snapshot::text,digest,confirmed_by,confirmed_at FROM baselines WHERE id=:id"
                ),
                {"id": baseline},
            ).one()
            == before
        )
        assert db.execute(
            text(
                "SELECT user_id,token_hash,csrf_token FROM sessions WHERE token_hash='prior-token-hash'"
            ),
            {},
        ).one() == (actor, "prior-token-hash", "prior-csrf")
        assert (
            db.execute(
                text("SELECT incident_id FROM investigation_runs WHERE id=:id"), {"id": run}
            ).scalar_one()
            == incident
        )
        assert db.execute(
            text("SELECT uid,active,retirement_reason FROM workloads WHERE id=:id"),
            {"id": workload},
        ).one() == ("prior-uid", True, None)
    command.check(config)
