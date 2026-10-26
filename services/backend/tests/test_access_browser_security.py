from datetime import timedelta
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from cloudops.entrypoints.api import app
from cloudops.features.access.dependencies import COOKIE
from cloudops.features.access.models import LoginSession
from cloudops.features.access.service import token_hash
from cloudops.features.incidents.models import Admission, Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job

ORIGIN = "https://cloudops.example.test"
CREDENTIALS = {"username": "operator", "password": "local-test-password"}


@pytest.fixture
def https_client(seeded, monkeypatch):
    monkeypatch.setenv("WEB_ORIGIN", ORIGIN)
    monkeypatch.setenv("COOKIE_SECURE", "true")
    settings.cache_clear()
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            yield client
    finally:
        settings.cache_clear()


def login(client):
    response = client.post("/api/login", json=CREDENTIALS, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response


def mutation_headers(response):
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def cookie(response):
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    return parsed[COOKIE]


def domain_counts():
    with sessions()() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (Incident, InvestigationRun, Job, Admission)
        )


def session_rows():
    with sessions()() as db:
        return tuple(
            db.execute(
                select(LoginSession.token_hash, LoginSession.expires_at).order_by(
                    LoginSession.token_hash
                )
            )
        )


def admit(client, headers):
    workload = client.get("/api/workloads").json()["items"][0]
    return client.post(
        "/api/incidents",
        json={
            "workload_id": workload["id"],
            "title": "Browser security fixture",
            "description": "",
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def assert_denied(response, code, status=403):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_https_cookie_round_trip_logout_and_server_invalidation(https_client, caplog):
    caplog.set_level("INFO")
    client = https_client
    signed_in = login(client)
    issued = cookie(signed_in)
    old_token = issued.value
    assert issued["secure"] and issued["httponly"]
    assert issued["samesite"] == "lax"
    assert issued["path"] == "/" and not issued["domain"]
    assert int(issued["max-age"]) == settings().session_seconds
    assert old_token not in signed_in.text
    assert signed_in.headers["cache-control"] == "no-store"
    stored_cookie = next(c for c in client.cookies.jar if c.name == COOKIE)
    assert not stored_cookie.domain_specified and stored_cookie.secure

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json() == signed_in.json()
    assert session.headers["cache-control"] == "no-store"
    assert "set-cookie" not in session.headers
    before = domain_counts()
    assert admit(client, mutation_headers(signed_in)).status_code == 201
    assert domain_counts() == tuple(value + 1 for value in before)

    # A Secure cookie must not be sent by the jar on HTTP or to another host.
    assert client.get("http://cloudops.example.test/api/session").status_code == 401
    assert client.get("https://foreign.example.test/api/session").status_code == 401
    assert client.get("/api/session").status_code == 200

    signed_out = client.post("/api/logout", headers=mutation_headers(signed_in))
    assert signed_out.status_code == 204
    deleted = cookie(signed_out)
    for attribute in ("path", "domain", "secure", "httponly", "samesite"):
        assert deleted[attribute] == issued[attribute]
    assert deleted.value == "" and deleted["max-age"] == "0" and deleted["expires"]
    assert not any(c.name == COOKIE for c in client.cookies.jar)
    assert client.get("/api/session").status_code == 401
    assert (
        client.get("/api/session", headers={"Cookie": f"{COOKIE}={old_token}"}).status_code == 401
    )
    with sessions()() as db:
        assert db.get(LoginSession, token_hash(old_token)) is None
    for authority in (old_token, signed_in.json()["csrf_token"], CREDENTIALS["password"]):
        assert authority not in caplog.text


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://foreign.example.test",
        "http://cloudops.example.test",
        ORIGIN + ":8443",
        ORIGIN + "/",
    ],
)
def test_login_requires_exact_origin_without_replacing_existing_session(https_client, origin):
    signed_in = login(https_client)
    before = session_rows()
    response = https_client.post(
        "/api/login", json=CREDENTIALS, headers={} if origin is None else {"Origin": origin}
    )
    assert_denied(response, "origin_denied")
    assert session_rows() == before
    assert https_client.get("/api/session").json() == signed_in.json()


def invalid_headers(case, signed_in):
    headers = mutation_headers(signed_in)
    if case == "missing_origin":
        headers.pop("Origin")
    elif case == "foreign_origin":
        headers["Origin"] = "https://foreign.example.test"
    elif case == "null_origin":
        headers["Origin"] = "null"
    elif case == "missing_csrf":
        headers.pop("X-CSRF-Token")
    elif case == "wrong_csrf":
        headers["X-CSRF-Token"] = "wrong-session-token"
    elif case == "cross_session_csrf":
        with TestClient(app, base_url=ORIGIN) as other:
            headers["X-CSRF-Token"] = login(other).json()["csrf_token"]
    return headers


@pytest.mark.parametrize("endpoint", ["incident", "logout"])
@pytest.mark.parametrize(
    "case",
    [
        "missing_origin",
        "foreign_origin",
        "null_origin",
        "missing_csrf",
        "wrong_csrf",
        "cross_session_csrf",
    ],
)
def test_mutation_denial_does_not_write_or_revoke(https_client, endpoint, case):
    signed_in = login(https_client)
    headers = invalid_headers(case, signed_in)
    before, before_sessions = domain_counts(), session_rows()
    response = (
        admit(https_client, headers)
        if endpoint == "incident"
        else https_client.post("/api/logout", headers=headers)
    )
    assert_denied(response, "origin_denied" if case.endswith("origin") else "csrf_denied")
    assert domain_counts() == before
    assert session_rows() == before_sessions
    assert https_client.get("/api/session").json() == signed_in.json()


@pytest.mark.parametrize("state", ["anonymous", "expired"])
def test_protected_mutations_require_a_live_session(https_client, state):
    signed_in = login(https_client)
    headers = mutation_headers(signed_in)
    if state == "anonymous":
        https_client.cookies.clear()
    else:
        with sessions()() as db, db.begin():
            db.execute(
                update(LoginSession).values(expires_at=database_now(db) - timedelta(seconds=1))
            )
    before = domain_counts()
    # Valid body and identity ensure this is an authentication denial, not validation.
    with sessions()() as db:
        from cloudops.features.workloads.models import Workload

        workload_id = str(db.scalar(select(Workload.id)))
    response = https_client.post(
        "/api/incidents",
        json={"workload_id": workload_id, "title": "Valid request", "description": ""},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert_denied(response, "session_expired", 401)
    assert_denied(https_client.post("/api/logout", headers=headers), "session_expired", 401)
    assert domain_counts() == before


def test_forwarded_headers_do_not_select_origin_or_cookie_policy(https_client):
    forged = {
        "Host": "attacker.test",
        "Forwarded": "host=attacker.test;proto=http",
        "X-Forwarded-Host": "attacker.test",
        "X-Forwarded-Proto": "http",
    }
    response = https_client.post(
        "/api/login", json=CREDENTIALS, headers={**forged, "Origin": "https://attacker.test"}
    )
    assert_denied(response, "origin_denied")
    assert session_rows() == ()
    response = https_client.post(
        "/api/login", json=CREDENTIALS, headers={**forged, "Origin": ORIGIN}
    )
    assert response.status_code == 200
    assert cookie(response)["secure"]
    assert not cookie(response)["domain"]


@pytest.mark.parametrize("header", ["Origin", "X-CSRF-Token"])
def test_duplicate_security_headers_are_denied(https_client, header):
    signed_in = login(https_client)
    headers = list(mutation_headers(signed_in).items())
    headers.append(
        (header, "https://foreign.example.test" if header == "Origin" else "wrong-token")
    )
    before = session_rows()
    assert_denied(
        https_client.post("/api/logout", headers=headers),
        "origin_denied" if header == "Origin" else "csrf_denied",
    )
    assert session_rows() == before


def test_non_ascii_csrf_is_a_denial_not_a_server_error(https_client):
    signed_in = login(https_client)
    headers = [(b"Origin", ORIGIN.encode()), (b"X-CSRF-Token", b"\xff")]
    before = session_rows()
    assert_denied(https_client.post("/api/logout", headers=headers), "csrf_denied")
    assert session_rows() == before
    assert https_client.get("/api/session").json() == signed_in.json()


def test_session_reads_do_not_extend_absolute_lifetime(https_client):
    signed_in = login(https_client)
    before = session_rows()
    for _ in range(2):
        response = https_client.get("/api/session")
        assert response.json()["expires_at"] == signed_in.json()["expires_at"]
        assert "set-cookie" not in response.headers
    assert session_rows() == before


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_explicit_http_loopback_remains_usable(seeded, monkeypatch, origin):
    monkeypatch.setenv("WEB_ORIGIN", origin)
    monkeypatch.setenv("COOKIE_SECURE", "false")
    settings.cache_clear()
    try:
        with TestClient(app, base_url=origin) as client:
            response = client.post("/api/login", json=CREDENTIALS, headers={"Origin": origin})
            assert response.status_code == 200
            issued = cookie(response)
            assert not issued["secure"] and issued["httponly"]
            assert issued["samesite"] == "lax" and issued["path"] == "/" and not issued["domain"]
            assert client.get("/api/session").status_code == 200
            headers = {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}
            assert admit(client, headers).status_code == 201
            response = client.post("/api/logout", headers=headers)
            assert response.status_code == 204
            assert not cookie(response)["secure"]
            assert client.get("/api/session").status_code == 401
    finally:
        settings.cache_clear()
