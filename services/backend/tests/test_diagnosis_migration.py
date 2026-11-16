"""Populated 0011 upgrade, repeatability and retained diagnosis downgrade guard."""

import json
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import sign_in
from knowledge_fixtures import write_corpus
from sqlalchemy import select, text

from cloudops.entrypoints.worker import run_once
from cloudops.features.access.models import User
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.shared.database import BACKEND_ROOT, engine, sessions
from test_diagnosis_lifecycle import admission, deployment as _deployment
from test_diagnosis_validation import context as _context

context, deployment = _context, _deployment


def fingerprint():
    with engine().connect() as connection:
        tables = connection.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename NOT LIKE 'diagnosis_%' AND tablename<>'alembic_version' ORDER BY tablename"
            )
        ).scalars()
        return {
            table: sorted(
                connection.execute(
                    text(
                        "SELECT (to_jsonb(t)"
                        + ("-'due_at'" if table == "jobs" else "")
                        + ")::text FROM "
                        + table
                        + " t"
                    )
                ).scalars()
            )
            for table in tables
        }


def test_populated_upgrade_preserves_all_existing_rows_and_bytes(client, tmp_path):
    headers = sign_in(client)
    workload = client.get("/api/workloads").json()["items"][0]["id"]
    created = client.post(
        "/api/incidents",
        json={"workload_id": workload, "title": "Retain original migration evidence"},
        headers=headers | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    identifier = created.json()["incident_id"]
    assert run_once()
    assert (
        client.post(
            f"/api/incidents/{identifier}/lifecycle",
            json={"action": "resolve", "reason": "Preserve original saved handoff"},
            headers=headers,
        ).status_code
        == 200
    )
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    with sessions()() as db, db.begin():
        actor = db.scalar(select(User.id).where(User.username == "operator"))
        db.add(
            RecommendationReport(
                workload_id=workload,
                created_by=actor,
                state="completed",
                result={"schema_version": "fixture-preserved", "message": "Original report bytes"},
            )
        )
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    before = fingerprint()
    command.downgrade(config, "0011_knowledge_selections")
    assert fingerprint() == before
    for _ in range(2):
        command.upgrade(config, "head")
    assert fingerprint() == before
    with engine().connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM diagnosis_analyses")) == 0
    command.check(config)
    assert json.loads(client.get(f"/api/incidents/{identifier}").text)["state"] == "resolved"


def test_downgrade_preserves_populated_analysis(db, context, deployment):
    from diagnosis_fake import serve

    with serve(lambda h: None) as server:
        deployment(server.url)
        admission(db, context)
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        with pytest.raises(RuntimeError, match="cannot be discarded"):
            command.downgrade(config, "0011_knowledge_selections")
        command.upgrade(config, "head")
