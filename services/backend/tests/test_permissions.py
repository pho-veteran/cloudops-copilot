"""Request-level evidence for the current operation/role contract."""

import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from permission_cases import CASES
from permission_inventory import FRAMEWORK_ROUTES, OPERATIONS, verify_inventory
from sqlalchemy import select, update

from cloudops.entrypoints.api import create_app
from cloudops.entrypoints.worker import run_once
from cloudops.features.access.dependencies import COOKIE
from cloudops.features.access.models import LoginSession, User
from cloudops.features.access.service import provision, token_hash
from cloudops.features.incidents.models import Admission, Incident
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, JobAttempt

ORIGIN = "http://localhost:5173"
ACTORS = ("anonymous", "expired", "viewer", "operator")
DOMAIN_MODELS = (Incident, InvestigationRun, Job, Admission, Evidence, JobAttempt)


@dataclass
class Actor:
    client: TestClient
    session: dict | None
    cookie: str | None

    @property
    def headers(self):
        return {
            "Origin": ORIGIN,
            **({"X-CSRF-Token": self.session["csrf_token"]} if self.session else {}),
        }


@dataclass
class Context:
    app: object
    actors: dict[str, Actor]
    workload_id: str
    incident_id: str
    run_id: str
    body: dict
    secrets: set[str]


def snapshot(models=DOMAIN_MODELS):
    # New connection observes committed writes, including accidental state-only changes.
    with sessions()() as db:
        return {
            model.__tablename__: sorted(
                (repr(dict(row)) for row in db.execute(select(model.__table__)).mappings())
            )
            for model in models
        }


def send(context, operation_id, actor_name, request=None):
    actor = context.actors[actor_name]
    operation = OPERATIONS[operation_id]
    request = dict(CASES[operation_id].request(context) if request is None else request)
    params = request.pop("path_params", {})
    path = operation.path.format(**params)
    headers = {**actor.headers, **request.pop("headers", {})}
    return actor.client.request(operation.method, path, headers=headers, **request)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == code


def assert_no_secrets(response, context, *, schema=False):
    for secret in context.secrets:
        assert secret not in response.text
    if (
        schema
        or not response.content
        or "application/json" not in response.headers.get("content-type", "")
    ):
        return

    def check(value):
        if isinstance(value, dict):
            assert not value.keys() & {
                "password",
                "password_hash",
                "token_hash",
                "session_token",
                "lease_token",
                "lease_until",
                "database_url",
            }
            for nested in value.values():
                check(nested)
        elif isinstance(value, list):
            for nested in value:
                check(nested)

    check(response.json())


@pytest.fixture
def permission_context(seeded, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "0")
    settings.cache_clear()
    with sessions()() as db:
        provision(db, "expired", "expired-test-password", "operator")
    app = create_app()
    assert not app.dependency_overrides
    with ExitStack() as stack:
        actors = {}
        for name in ACTORS:
            client = stack.enter_context(TestClient(app))
            result, cookie = None, None
            if name != "anonymous":
                response = client.post(
                    "/api/login",
                    json={
                        "username": name,
                        "password": "expired-test-password"
                        if name == "expired"
                        else "local-test-password",
                    },
                    headers={"Origin": ORIGIN},
                )
                assert response.status_code == 200, response.text
                result, cookie = response.json(), client.cookies[COOKIE]
            actors[name] = Actor(client, result, cookie)

        operator = actors["operator"]
        workload = operator.client.get("/api/workloads").json()["items"][0]
        body = {
            "workload_id": workload["id"],
            "title": "Permission fixture incident",
            "description": "Giữ nguyên mô tả để kiểm tra quyền đọc.",
        }
        response = operator.client.post(
            "/api/incidents",
            json=body,
            headers={**operator.headers, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        ids = response.json()
        assert run_once()

        with sessions()() as db, db.begin():
            db.execute(
                update(LoginSession)
                .where(LoginSession.token_hash == token_hash(actors["expired"].cookie))
                .values(expires_at=database_now(db) - timedelta(seconds=1))
            )
        secrets = {"local-test-password", "expired-test-password", settings().database_url}
        secrets.update(actor.cookie for actor in actors.values() if actor.cookie)
        with sessions()() as db:
            secrets.update(db.scalars(select(User.password_hash)))
            secrets.update(db.scalars(select(LoginSession.token_hash)))
            secrets.update(str(value) for value in db.scalars(select(JobAttempt.lease_token)))
            assert db.scalar(select(JobAttempt.lease_token)) is not None
        yield Context(app, actors, workload["id"], ids["incident_id"], ids["run_id"], body, secrets)


def test_available_route_inventory():
    verify_inventory(create_app(), OPERATIONS, CASES)


@pytest.mark.parametrize("operation_id", tuple(OPERATIONS))
@pytest.mark.parametrize("actor_name", ACTORS)
def test_operation_role_matrix(permission_context, operation_id, actor_name):
    context = permission_context
    category = OPERATIONS[operation_id].category
    before = snapshot()
    accounts_before = snapshot((User, LoginSession))
    response = send(context, operation_id, actor_name)
    assert_no_secrets(response, context)
    denied = False
    if category not in {"account_entry", "public_probe"} and actor_name in {"anonymous", "expired"}:
        assert_error(response, 401, "session_expired")
        denied = True
    elif category == "operator_mutation" and actor_name == "viewer":
        assert_error(response, 403, "permission_denied")
        denied = True
    else:
        CASES[operation_id].check(response, context, actor_name)

    if denied or category in {"protected_read", "public_probe"}:
        assert snapshot() == before
        assert snapshot((User, LoginSession)) == accounts_before
    elif category == "operator_mutation":
        assert_admission(context, operation_id, response, before)
        assert snapshot((User, LoginSession)) == accounts_before
    elif category == "session_self_service":
        assert snapshot() == before
        actor = context.actors[actor_name]
        accounts_after = snapshot((User, LoginSession))
        assert accounts_after["users"] == accounts_before["users"]
        assert accounts_after["sessions"] == [
            row for row in accounts_before["sessions"] if token_hash(actor.cookie) not in row
        ]
        with sessions()() as db:
            assert db.get(LoginSession, token_hash(actor.cookie)) is None
            for other in {"viewer", "operator"} - {actor_name}:
                assert db.get(LoginSession, token_hash(context.actors[other].cookie)) is not None
        assert_error(actor.client.get("/api/session"), 401, "session_expired")
        actor.client.cookies.set(COOKIE, actor.cookie)
        assert_error(actor.client.get("/api/session"), 401, "session_expired")
    elif category == "account_entry":
        assert snapshot() == before
        result = response.json()
        token = context.actors[actor_name].client.cookies[COOKIE]
        assert token not in response.text
        with sessions()() as db:
            created = db.get(LoginSession, token_hash(token))
            assert str(created.user_id) == result["principal"]["id"]


def assert_admission(context, operation_id, response, before):
    ids = response.json()
    after = snapshot()
    for model in DOMAIN_MODELS:
        name = model.__tablename__
        delta = 1 if model in (InvestigationRun, Job, Admission) else 0
        if model is Incident and operation_id == "create_incident":
            delta = 1
        assert len(after[name]) == len(before[name]) + delta
        assert set(before[name]) <= set(after[name]), f"Existing {name} modified by admission"
    with sessions()() as db:
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        incident = db.get(Incident, UUID(ids["incident_id"]))
        assert str(run.incident_id) == ids["incident_id"]
        assert run.state == "queued"
        assert str(run.requested_by) == context.actors["operator"].session["principal"]["id"]
        job = db.scalar(select(Job).where(Job.subject_id == run.id))
        assert job.state == "queued"
        admission = db.scalar(select(Admission).where(Admission.run_id == run.id))
        assert admission.actor_id == run.requested_by
        if operation_id == "create_run":
            assert str(incident.id) == context.incident_id
        else:
            assert str(incident.id) != context.incident_id
            assert incident.created_by == run.requested_by
            assert str(incident.workload_id) == context.workload_id


@pytest.mark.parametrize("operation_id", ["create_incident", "create_run"])
def test_viewer_denial_leaves_no_admission_before_operator_success(
    permission_context, operation_id
):
    context = permission_context
    request = CASES[operation_id].request(context)
    before = snapshot()
    assert_error(send(context, operation_id, "viewer", request), 403, "permission_denied")
    assert snapshot() == before
    with sessions()() as db:
        assert (
            db.scalar(
                select(Admission).where(
                    Admission.key == UUID(request["headers"]["Idempotency-Key"])
                )
            )
            is None
        )
    response = send(context, operation_id, "operator", request)
    CASES[operation_id].check(response, context, "operator")
    assert_admission(context, operation_id, response, before)


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_login_validates_credentials_and_returns_the_account_role(permission_context, role):
    context = permission_context
    client = context.actors["anonymous"].client
    before = snapshot((User, LoginSession))
    response = client.post(
        "/api/login",
        json={"username": role, "password": "wrong-password"},
        headers={"Origin": ORIGIN},
    )
    assert_error(response, 401, "invalid_credentials")
    assert_no_secrets(response, context)
    assert snapshot((User, LoginSession)) == before
    response = client.post(
        "/api/login",
        json={"username": role, "password": "local-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["principal"]["role"] == role
    assert response.json()["principal"]["id"] == context.actors[role].session["principal"]["id"]
    assert_no_secrets(response, context)


@pytest.mark.parametrize("actor_name", ["viewer", "operator"])
def test_run_is_scoped_to_its_incident(permission_context, actor_name):
    context = permission_context
    response = send(context, "create_incident", "operator")
    assert response.status_code == 201
    other = response.json()["incident_id"]
    before = snapshot()
    response = context.actors[actor_name].client.get(
        f"/api/incidents/{other}/runs/{context.run_id}"
    )
    assert_error(response, 404, "run_not_found")
    assert_no_secrets(response, context)
    listed = context.actors[actor_name].client.get(f"/api/incidents/{other}/runs")
    assert listed.status_code == 200
    assert {item["incident_id"] for item in listed.json()["items"]} == {other}
    assert context.run_id not in {item["id"] for item in listed.json()["items"]}
    assert_no_secrets(listed, context)
    assert snapshot() == before


@pytest.mark.parametrize("path", tuple(FRAMEWORK_ROUTES))
@pytest.mark.parametrize("actor_name", ACTORS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_framework_public_routes(permission_context, path, actor_name, method):
    context = permission_context
    before = snapshot()
    response = context.actors[actor_name].client.request(method, path)
    assert response.status_code == 200, response.text
    assert_no_secrets(response, context, schema=True)
    if method == "HEAD":
        assert response.content == b""
    elif path == "/openapi.json":
        document = response.json()
        assert document["info"]["title"] == "CloudOps Copilot"
        assert "paths" in document and "components" in document
        assert "application/json" in response.headers["content-type"]
    else:
        assert "text/html" in response.headers["content-type"]
    for value in (context.incident_id, context.run_id, context.body["description"]):
        assert value not in response.text
    assert snapshot() == before


@pytest.mark.parametrize("actor_name", ACTORS)
def test_readiness_failure_is_public_and_sanitized(permission_context, monkeypatch, actor_name):
    from cloudops.features.health import api

    def unavailable():
        raise RuntimeError("postgresql://sensitive-readiness-fixture")

    monkeypatch.setattr(api, "require_schema", unavailable)
    context = permission_context
    before = snapshot()
    response = send(context, "readiness", actor_name)
    assert_error(response, 503, "database_not_ready")
    assert "sensitive-readiness-fixture" not in response.text
    assert_no_secrets(response, context)
    assert snapshot() == before


def test_secret_assertions_have_a_real_persisted_lease_sentinel(permission_context):
    context = permission_context
    with sessions()() as db:
        attempt = db.scalar(select(JobAttempt))
        lease = str(attempt.lease_token)
        assert lease in context.secrets
    response = send(context, "get_run", "viewer")
    assert response.json()["attempts"]
    assert response.json()["evidence"][0]["sha256"]
    assert_no_secrets(response, context)
    assert "lease_token" not in json.dumps(response.json())
