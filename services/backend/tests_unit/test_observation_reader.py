"""Controlled HTTP transport evidence; does not claim live TLS/cluster validation."""

import asyncio
import base64
import json
import ssl
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from dataclasses import replace
from time import monotonic

import httpx
import pytest
from observation_fixtures import objects, observed

from cloudops.features.workloads import observation_reader as module
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.features.workloads.observation_reader import ObservationReader


class Stream(httpx.AsyncByteStream):
    def __init__(self, body, delay=0):
        self.body, self.delay, self.closed = body, delay, False

    async def __aiter__(self):
        for byte in self.body:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / "reader.json"
    path.write_text(
        json.dumps(
            {
                "server": "https://fixed.test:6443",
                "token": "private-token-sentinel",
                "ca_data": base64.b64encode(b"controlled-ca").decode(),
            }
        )
    )
    monkeypatch.setenv("KUBE_READ_CONFIG", str(path))
    context = ssl.create_default_context()
    monkeypatch.setattr(module.ssl, "create_default_context", lambda **_: context)
    return observed()


def response(value, status=200, headers=None):
    return httpx.Response(status, stream=Stream(json.dumps(value).encode()), headers=headers)


def test_fixed_get_paths_and_sanitized_response(configured):
    deployment, rs, pods = objects()
    requests = []

    async def send(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "fixed.test"
        if request.url.path.endswith("replicasets"):
            return response(rs)
        if request.url.path.endswith("pods"):
            return response(pods)
        return response(deployment)

    result = asyncio.run(
        ObservationReader(transport=httpx.MockTransport(send)).read(
            configured.identity, configured.started_at
        )
    )
    assert len(requests) == 4 and result.health == "healthy"
    assert "private-token-sentinel" not in result.model_dump_json()


@pytest.mark.parametrize(
    "status,reason",
    [
        (403, "permission_denied"),
        (401, "permission_denied"),
        (404, "target_changed"),
        (410, "pagination_invalid"),
        (503, "cluster_unavailable"),
        (302, "cluster_unavailable"),
    ],
)
def test_read_failures_do_not_passthrough_provider_or_follow_redirect(configured, status, reason):
    calls = []

    def send(request):
        calls.append(request)
        return response(
            {"message": "private-provider-sentinel"},
            status,
            {"Location": "https://foreign.test/secret"},
        )

    with pytest.raises(ObservationError) as error:
        asyncio.run(
            ObservationReader(transport=httpx.MockTransport(send)).read(
                configured.identity, configured.started_at
            )
        )
    assert error.value.code == reason and len(calls) == 1
    assert "private-provider-sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("continue_loop", "pagination_invalid"),
        ("version_change", "pagination_invalid"),
        ("cap", "budget_exhausted"),
        ("compressed", "payload_limit"),
        ("oversize", "payload_limit"),
    ],
)
def test_pagination_and_payload_bounds(configured, monkeypatch, case, reason):
    deployment, rs, pods = objects()
    pages, requests = 0, []

    async def send(request):
        nonlocal pages
        requests.append(request)
        if request.url.path.endswith("replicasets"):
            pages += 1
            value = {
                "kind": "ReplicaSetList",
                "apiVersion": "apps/v1",
                "metadata": {
                    "resourceVersion": str(pages) if case == "version_change" else "opaque",
                    "continue": "same" if case == "continue_loop" else str(pages),
                },
                "items": [],
            }
            return response(value)
        if case == "compressed":
            return response(deployment, headers={"Content-Encoding": "gzip"})
        if case == "oversize":
            return httpx.Response(200, stream=Stream(b"x" * 30))
        return response(deployment)

    if case == "oversize":
        monkeypatch.setattr(module, "POLICY", replace(module.POLICY, response_bytes=20))
    with pytest.raises(ObservationError) as error:
        asyncio.run(
            ObservationReader(transport=httpx.MockTransport(send)).read(
                configured.identity, configured.started_at
            )
        )
    assert error.value.code == reason and len(requests) <= 3


def test_drip_feed_total_deadline_cancels_and_closes_stream(configured, monkeypatch):
    monkeypatch.setattr(module, "POLICY", replace(module.POLICY, deadline_seconds=0.05))
    stream = Stream(b"{" * 100, delay=0.01)
    start = monotonic()
    with pytest.raises(ObservationError) as error:
        asyncio.run(
            ObservationReader(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
            ).read(configured.identity, configured.started_at)
        )
    assert error.value.code == "attempt_timeout"
    assert monotonic() - start < 1 and stream.closed


def test_uid_mismatch_stops_before_related_resource_reads(configured):
    deployment, _, _ = objects()
    deployment["metadata"]["uid"] = "replacement"
    calls = []

    def send(request):
        calls.append(request)
        return response(deployment)

    with pytest.raises(ObservationError) as error:
        asyncio.run(
            ObservationReader(transport=httpx.MockTransport(send)).read(
                configured.identity, configured.started_at
            )
        )
    assert error.value.code == "target_changed" and len(calls) == 1


def test_real_tls_drip_feed_deadline_closes_the_socket(tmp_path, monkeypatch):
    openssl = shutil.which("openssl") or "C:/Program Files/Git/usr/bin/openssl.exe"
    assert Path(openssl).is_file(), "Install OpenSSL to exercise the real TLS cancellation gate."
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    received, closed = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            received.set()
            try:
                for _ in range(1000):
                    self.wfile.write(b"{")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                closed.set()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = tmp_path / "client.json"
    connection.write_text(
        json.dumps(
            {
                "server": f"https://127.0.0.1:{server.server_port}",
                "token": "test-only",
                "ca_data": base64.b64encode(cert.read_bytes()).decode(),
            }
        )
    )
    monkeypatch.setenv("KUBE_READ_CONFIG", str(connection))
    monkeypatch.setattr(module, "POLICY", replace(module.POLICY, deadline_seconds=1))
    value = observed()
    try:
        start = monotonic()
        with pytest.raises(ObservationError) as error:
            asyncio.run(ObservationReader().read(value.identity, value.started_at))
        assert error.value.code == "attempt_timeout"
        assert monotonic() - start < 4
        assert received.is_set() and closed.wait(2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
