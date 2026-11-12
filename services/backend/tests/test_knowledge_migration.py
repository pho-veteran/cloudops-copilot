from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from cloudops.features.knowledge.indexing import index_corpus
from cloudops.shared.database import engine
from knowledge_fixtures import write_corpus

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_populated_0009_upgrade_is_repeatable_and_preserves_legacy_rows():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0009_job_checkpoints")
    user_id = uuid4()
    with engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (:id, :username, :password_hash, 'viewer')"
            ),
            {
                "id": user_id,
                "username": "migration-owner",
                "password_hash": "not-a-real-hash",
            },
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine().connect() as connection:
        row = connection.execute(
            text("SELECT username, role FROM users WHERE id = :id"), {"id": user_id}
        ).first()
        assert row == ("migration-owner", "viewer")
        heads = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        assert heads == ["0011_knowledge_selections"]


def test_downgrade_refuses_to_drop_retained_knowledge_history(db, tmp_path):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, "0009_job_checkpoints")
    command.upgrade(config, "head")
