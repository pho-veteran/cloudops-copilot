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
    assert ScriptDirectory.from_config(config).get_heads() == ["0004_workload_scope"]
    command.check(config)
