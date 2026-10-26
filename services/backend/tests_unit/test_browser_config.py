import pytest
from fastapi.testclient import TestClient

from cloudops.shared.config import browser_origin, cookie_secure, settings


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/unused_test")
    monkeypatch.setenv("WEB_ORIGIN", "https://cloudops.example.test")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("SESSION_SECONDS", "28800")
    monkeypatch.setenv("COLLECTOR_DELAY_SECONDS", "2")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.mark.parametrize(
    ("origin", "secure"),
    [
        ("http://localhost:5173", False),
        ("http://127.0.0.1:5173", False),
        ("http://localhost", False),
        ("http://127.0.0.1", False),
        ("https://cloudops.example.test", True),
        ("https://cloudops.example.test:8443", True),
        ("https://cloudops.example.test:1", True),
        ("https://cloudops.example.test:65535", True),
        ("https://localhost", True),
        ("https://127.0.0.1", True),
        ("https://[::1]", True),
        ("https://[2001:db8::1]:8443", True),
        ("https://[::ffff:7f00:1]", True),
        ("https://xn--bcher-kva.example", True),
        ("https://cloudops.example.test.", True),
        ("https://" + ".".join(["a" * 63] * 3 + ["a" * 61]) + ".", True),
    ],
)
def test_supported_origins(origin, secure):
    assert browser_origin(origin, secure) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "null",
        "*",
        "example.test",
        "//example.test",
        "ftp://example.test",
        "https:///example.test",
        "https://",
        "https://:8443",
        "https://user:secret@example.test",
        "https://@example.test",
        "https://example.test/",
        "https://example.test/path",
        "https://example.test?query=secret",
        "https://example.test?",
        "https://example.test#secret",
        "https://example.test#",
        "https://example.test:",
        "https://example.test:abc",
        "https://example.test:0",
        "https://example.test:65536",
        "https://example.test:-1",
        "https://example.test:443",
        "https://example.test:08443",
        "http://localhost:80",
        "HTTPS://example.test",
        "https://Example.test",
        "https://example.test,https://other.test",
        "https://example.test https://other.test",
        " https://example.test",
        "https://example.test ",
        "\x00https://example.test",
        "https://exam\tple.test",
        "https://example.test\n",
        "https://example.test\r",
        "https://example.test\x7f",
        "https://example.test\\evil",
        "https://%65xample.test",
        "https://xn--abc.test",
        "https://xn--.test",
        "https://bücher.example",
        "https://exa_mple.test",
        "https://-example.test",
        "https://example-.test",
        "https://example..test",
        "https://" + "a" * 64 + ".test",
        "https://" + ".".join(["a" * 63] * 4),
        "https://[::1",
        "https://[::1]junk",
        "https://[v1.host]",
        "https://[fe80::1%25eth0]",
        "https://[2001:0db8::1]",
        "https://[2001:DB8::1]",
        "https://127.1",
        "https://2130706433",
        "https://0x7f000001",
        "https://127.000.000.001",
        "https://256.1.1.1",
    ],
)
def test_invalid_origin_is_rejected_without_echoing_input(origin):
    with pytest.raises(RuntimeError, match="WEB_ORIGIN") as error:
        browser_origin(origin, True)
    assert "secret" not in str(error.value)
    assert error.value.__context__ is None or error.value.__suppress_context__


@pytest.mark.parametrize(
    ("origin", "secure"),
    [
        ("http://localhost:5173", True),
        ("http://127.0.0.1:5173", True),
        ("http://example.test", True),
        ("http://example.test", False),
        ("http://0.0.0.0:5173", False),
        ("http://192.168.1.10:5173", False),
        ("http://app.localhost:5173", False),
        ("http://[::1]:5173", False),
        ("https://localhost", False),
        ("https://example.test", False),
    ],
)
def test_insecure_or_contradictory_combinations(origin, secure):
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        browser_origin(origin, secure)


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("False", False)])
def test_boolean(value, expected):
    assert cookie_secure(value) is expected


@pytest.mark.parametrize("value", ["", "yes", "no", "1", "0", " true", "false "])
def test_invalid_boolean(value):
    with pytest.raises(RuntimeError, match="COOKIE_SECURE must be true or false"):
        cookie_secure(value)


@pytest.mark.parametrize("seconds", ["0", "28801", "-1", "secret", "1.5"])
def test_invalid_session_lifetime(monkeypatch, seconds):
    monkeypatch.setenv("SESSION_SECONDS", seconds)
    with pytest.raises(RuntimeError, match="SESSION_SECONDS") as error:
        settings()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("seconds", ["1", "28800"])
def test_absolute_session_lifetime_bounds(monkeypatch, seconds):
    monkeypatch.setenv("SESSION_SECONDS", seconds)
    assert settings().session_seconds == int(seconds)


@pytest.mark.parametrize(
    ("name", "value"),
    [("WEB_ORIGIN", "https://user:secret@example.test"), ("COOKIE_SECURE", "secret")],
)
def test_invalid_configuration_stops_api_before_database(monkeypatch, name, value):
    from cloudops.entrypoints import api

    monkeypatch.setenv(name, value)

    def unexpected_database_access():
        pytest.fail("Invalid browser configuration reached the database startup check")

    monkeypatch.setattr(api, "require_schema", unexpected_database_access)
    with pytest.raises(RuntimeError, match=name) as error:
        with TestClient(api.create_app()):
            pytest.fail("Invalid browser configuration started serving requests")
    assert "secret" not in str(error.value)
