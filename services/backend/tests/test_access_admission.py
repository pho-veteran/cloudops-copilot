from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from alembic import command
from alembic.config import Config
from conftest import sign_in
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from cloudops.features.access.models import LoginSession
from cloudops.features.access.service import provision
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import BACKEND_ROOT, sessions
from cloudops.shared.jobs import Job


def incident_body(client):
    workload = client.get("/api/workloads").json()["items"][0]
    return {
        "workload_id": workload["id"],
        "title": "Memory issue",
        "description": "Giữ nguyên mô tả của người dùng.",
    }


def create(client, headers, body, key=None):
    return client.post(
        "/api/incidents", json=body, headers={**headers, "Idempotency-Key": str(key or uuid4())}
    )


def test_access_origin_csrf_viewer_and_validation(client):
    assert client.get("/api/incidents").status_code == 401
    assert (
        client.post(
            "/api/login", json={"username": "operator", "password": "local-test-password"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/login",
            json={"username": "operator", "password": "wrong"},
            headers={"Origin": "http://localhost:5173"},
        ).status_code
        == 401
    )
    headers = sign_in(client)
    body = incident_body(client)
    assert create(client, {}, body).status_code == 403
    assert create(client, {**headers, "Origin": "http://evil.test"}, body).status_code == 403
    assert create(client, headers, {**body, "title": "  "}).status_code == 422
    assert create(client, headers, {**body, "workload_id": str(uuid4())}).status_code == 422
    assert client.get("/api/incidents?limit=101").status_code == 422
    created = create(client, headers, body).json()
    headers = sign_in(client, "viewer")
    assert client.get("/api/incidents").status_code == 200
    assert (
        client.get(f"/api/incidents/{created['incident_id']}/runs/{created['run_id']}").status_code
        == 200
    )
    assert create(client, headers, body).status_code == 403
    assert (
        client.post(
            f"/api/incidents/{created['incident_id']}/runs",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 403
    )


def test_session_cookie_logout_expiry_and_reset(client, db):
    headers = sign_in(client)
    assert (
        "HttpOnly"
        in client.post(
            "/api/login",
            json={"username": "operator", "password": "local-test-password"},
            headers=headers,
        ).headers["set-cookie"]
    )
    headers = {
        "Origin": "http://localhost:5173",
        "X-CSRF-Token": client.get("/api/session").json()["csrf_token"],
    }
    assert client.post("/api/logout", headers=headers).status_code == 204
    assert client.get("/api/session").status_code == 401
    sign_in(client)
    with db.begin():
        db.execute(
            update(LoginSession).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
    assert client.get("/api/session").status_code == 401
    sign_in(client)
    provision(db, "operator", "replacement-test-password", "viewer")
    assert client.get("/api/session").status_code == 401


def test_atomic_idempotent_admission_and_migration_preserves_data(client, db):
    headers = sign_in(client)
    body = incident_body(client)
    key = uuid4()
    first = create(client, headers, body, key)
    assert first.status_code == 201, first.text
    assert create(client, headers, body, key).json() == first.json()
    assert create(client, headers, {**body, "title": "Different"}, key).status_code == 409
    run_url = f"/api/incidents/{first.json()['incident_id']}/runs"
    assert (
        client.post(run_url, headers={**headers, "Idempotency-Key": str(uuid4())}).status_code
        == 409
    )
    for model in [Incident, InvestigationRun, Job]:
        assert db.scalar(select(func.count()).select_from(model)) == 1
    db.rollback()
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    with TestClient(client.app) as restarted:
        restarted.cookies.update(client.cookies)
        detail = restarted.get(f"/api/incidents/{first.json()['incident_id']}")
        assert detail.json()["description"] == body["description"]


def test_concurrent_duplicate_creates_one_run(client, db):
    headers = sign_in(client)
    body = incident_body(client)
    key = uuid4()
    cookies = dict(client.cookies)

    def send(_):
        with TestClient(client.app) as other:
            other.cookies.update(cookies)
            return create(other, headers, body, key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(send, range(4)))
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["run_id"] for r in responses}) == 1
    with sessions()() as check:
        assert check.scalar(select(func.count()).select_from(Job)) == 1
