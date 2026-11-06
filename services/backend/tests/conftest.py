import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from cloudops.shared.config import settings
from cloudops.shared.database import engine, sessions


@pytest.fixture(scope="session", autouse=True)
def test_database():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or not urlsplit(url).path.endswith("_test"):
        pytest.fail(
            "Set TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test; tests clear its data."
        )
    os.environ.update(
        DATABASE_URL=url,
        COOKIE_SECURE="false",
        COLLECTION_SOURCE_MODE="development_fixture",
        WEB_ORIGIN="http://localhost:5173",
        WORKLOADS_FILE=str(
            Path(__file__).resolve().parents[3] / "config/development-workloads.json"
        ),
    )
    settings.cache_clear()
    engine.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    yield
    engine().dispose()


@pytest.fixture(autouse=True)
def clean_database(test_database):
    with engine().begin() as connection:
        tables = connection.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"
            )
        )
        names = [f'"{row[0]}"' for row in tables]
        if names:
            connection.execute(text("TRUNCATE " + ",".join(names) + " CASCADE"))
    settings.cache_clear()


@pytest.fixture(autouse=True)
def dispatch_permit(tmp_path_factory):
    """A fresh trusted normal-mode permit for every test; subprocess workers inherit it."""
    from cloudops.entrypoints.dispatch import CLASSES
    from cloudops.shared.dispatch_permit import bootstrap

    root = tmp_path_factory.mktemp("dispatch")
    bootstrap(
        root,
        CLASSES,
        reason="Test fresh install",
        evidence=["pytest fixture"],
        actor={"uid": None, "channel": "test_fixture"},
    )
    previous = os.environ.get("JOB_DISPATCH_ROOT")
    os.environ["JOB_DISPATCH_ROOT"] = str(root)
    yield root
    if previous is None:
        os.environ.pop("JOB_DISPATCH_ROOT", None)
    else:
        os.environ["JOB_DISPATCH_ROOT"] = previous


@pytest.fixture
def db():
    with sessions()() as session:
        yield session


@pytest.fixture
def seeded(db):
    from cloudops.features.access.service import provision
    from cloudops.features.workloads.service import sync_config

    provision(db, "operator", "local-test-password", "operator")
    provision(db, "viewer", "local-test-password", "viewer")
    sync_config(db)


@pytest.fixture
def client(seeded):
    from cloudops.entrypoints.api import app

    with TestClient(app) as client:
        yield client


def sign_in(client, username="operator"):
    response = client.post(
        "/api/login",
        json={"username": username, "password": "local-test-password"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    return {"Origin": "http://localhost:5173", "X-CSRF-Token": response.json()["csrf_token"]}
