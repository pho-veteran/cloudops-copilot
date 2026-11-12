from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cloudops.features.diagnosis.schemas import CallKind
from cloudops.features.diagnosis.transport import ModelClient
from diagnosis_fake import gzip_payload, raw_trickle, respond, serve, serve_tls
from diagnosis_fixtures import profile, text_payload

TOKEN_ENV = {"DIAGNOSIS_TEST_TOKEN": "synthetic-test-token"}
MESSAGES = [{"role": "user", "content": "Reply OK"}]


def _client(url: str, *, limits=None, ca_file=None, proxy=None, environ=None):
    overrides = {"base_url": url, "allow_insecure_loopback": True}
    if limits:
        overrides["limits"] = limits
    if ca_file:
        overrides["tls"] = {"ca_file": str(ca_file)}
    if proxy:
        overrides["proxy"] = {"url": proxy}
    return ModelClient(profile(**overrides), environ=environ or TOKEN_ENV)


def _send(url: str, **kwargs):
    return _client(url, **kwargs).send(MESSAGES, before_send=lambda: None)


def _text(handler):
    respond(handler, text_payload())


def test_real_server_text_capture():
    with serve(_text) as server:
        result = _send(server.url + "/v1")
        assert result.kind is CallKind.TEXT
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request["path"] == "/v1/chat/completions"
        body = json.loads(request["body"])
        assert body["stream"] is False
        assert body["max_tokens"] == 32


def test_header_trickle_is_bounded_by_deadline():
    with raw_trickle() as server:
        started = time.monotonic()
        result = _send(
            server.url + "/v1",
            limits={"deadline_seconds": 1.0, "read_timeout_seconds": 0.2},
        )
        elapsed = time.monotonic() - started
        assert result.kind is CallKind.TIMEOUT
        assert elapsed < 2.0


def test_body_trickle_is_bounded_by_deadline():
    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.05)

    with serve(trickle) as server:
        started = time.monotonic()
        result = _send(
            server.url + "/v1",
            limits={"deadline_seconds": 1.0, "read_timeout_seconds": 0.2},
        )
        assert result.kind is CallKind.TIMEOUT
        assert time.monotonic() - started < 2.0


def test_declared_oversize_and_chunked_oversize():
    def declared(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "999999")
        handler.end_headers()

    with serve(declared) as server:
        result = _send(server.url + "/v1", limits={"max_response_bytes": 64})
        assert result.kind is CallKind.RESPONSE_TOO_LARGE

    def chunked(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for _ in range(4):
            part = b" " * 100_000
            handler.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")

    with serve(chunked) as server:
        result = _send(server.url + "/v1", limits={"max_response_bytes": 64})
        assert result.kind is CallKind.RESPONSE_TOO_LARGE


def test_gzip_bomb_and_unsupported_encoding():
    payload = text_payload("x")
    payload["padding"] = " " * 300_000
    body, headers = gzip_payload(payload)
    with serve(lambda handler: respond(handler, None, body=body, headers=headers)) as server:
        result = _send(server.url + "/v1", limits={"max_response_bytes": 1024})
        assert result.kind is CallKind.RESPONSE_TOO_LARGE

    with serve(
        lambda handler: respond(
            handler,
            text_payload(),
            headers={"Content-Encoding": "unsupported"},
        )
    ) as server:
        result = _send(server.url + "/v1")
        assert result.kind is CallKind.INVALID_ENCODING


def test_redirect_404_and_sse_classification():
    with serve(
        lambda handler: respond(
            handler, {}, status=308, headers={"Location": "/v1/chat/completions"}
        )
    ) as server:
        result = _send(server.url + "/v1")
        assert result.kind is CallKind.UNEXPECTED_REDIRECT
        assert len(server.requests) == 1

    with serve(
        lambda handler: respond(
            handler, None, status=404, body=b"<html>no</html>", content_type="text/html"
        )
    ) as server:
        result = _send(server.url + "/v1")
        assert result.kind is CallKind.REQUEST_REJECTED

    with serve(
        lambda handler: respond(
            handler, None, body=b"data: {}\n\n", content_type="text/event-stream"
        )
    ) as server:
        result = _send(server.url + "/v1")
        assert result.kind is CallKind.INVALID_CONTENT_TYPE


def test_connection_refused_and_ambient_proxy_ignored(monkeypatch):
    result = _send("http://127.0.0.1:1/v1")
    assert result.kind is CallKind.TRANSPORT_ERROR

    with serve(lambda handler: respond(handler, {}, status=502)) as recorder:
        monkeypatch.setenv("HTTP_PROXY", recorder.url)
        monkeypatch.setenv("HTTPS_PROXY", recorder.url)
        monkeypatch.setenv("ALL_PROXY", recorder.url)
        with serve(_text) as target:
            result = _send(target.url + "/v1")
            assert result.kind is CallKind.TEXT
            assert recorder.requests == []


def test_explicit_proxy_is_used():
    with serve(lambda handler: respond(handler, {}, status=502)) as proxy:
        with serve(_text) as target:
            result = _send(target.url + "/v1", proxy=proxy.url)
            assert result.kind is CallKind.UPSTREAM_ERROR
            assert proxy.requests


def test_tls_verification_and_ca_override(tmp_path: Path):
    try:
        with serve_tls(_text, tmp_path) as server:
            without_ca = _send(server.url + "/v1")
            assert without_ca.kind is CallKind.TRANSPORT_ERROR
            with_ca = _send(server.url + "/v1", ca_file=server.ca_file)
            assert with_ca.kind is CallKind.TEXT
    except RuntimeError as exc:
        pytest.skip(str(exc))
