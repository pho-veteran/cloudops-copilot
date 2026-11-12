"""Local-only OpenAI-compatible fake transports and servers for diagnosis tests."""

from __future__ import annotations

import gzip
import json
import shutil
import socketserver
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx


@dataclass
class FakeServer:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    ca_file: Path | None = None
    _server: Any = None
    _thread: threading.Thread | None = None

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "FakeServer":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def mock(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _handler_class(handler: Callable[[BaseHTTPRequestHandler], None]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length) if length else b""
            self.server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            handler(self)

        def do_GET(self):
            self.server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": b"",
                }
            )
            handler(self)

        def log_message(self, *_):
            pass

    return Handler


def serve(
    handler: Callable[[BaseHTTPRequestHandler], None], *, host: str = "127.0.0.1"
) -> FakeServer:
    server = ThreadingHTTPServer((host, 0), _handler_class(handler))
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return FakeServer(
        url=f"http://{host}:{server.server_port}",
        requests=server.requests,
        _server=server,
        _thread=thread,
    )


def _openssl() -> str | None:
    return shutil.which("openssl") or (
        "C:/Program Files/Git/usr/bin/openssl.exe"
        if Path("C:/Program Files/Git/usr/bin/openssl.exe").is_file()
        else None
    )


def serve_tls(
    handler: Callable[[BaseHTTPRequestHandler], None],
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
) -> FakeServer:
    openssl = _openssl()
    if openssl is None:
        raise RuntimeError("OpenSSL is required for the local TLS test")
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
    server = ThreadingHTTPServer((host, 0), _handler_class(handler))
    server.requests = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return FakeServer(
        url=f"https://{host}:{server.server_port}",
        requests=server.requests,
        ca_file=cert,
        _server=server,
        _thread=thread,
    )


class _RawHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.sendall(b"HTTP/1.1 200 OK\r\n")
            for value in b"X-Slow: " + (b"a" * 80) + b"\r\n\r\n":
                self.request.sendall(bytes([value]))
                threading.Event().wait(0.05)
        except OSError:
            pass


def raw_trickle(*, host: str = "127.0.0.1") -> FakeServer:
    server = socketserver.ThreadingTCPServer((host, 0), _RawHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return FakeServer(
        url=f"http://{host}:{server.server_address[1]}",
        _server=server,
        _thread=thread,
    )


def respond(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    content_type: str = "application/json",
) -> None:
    data = body if body is not None else json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def gzip_payload(payload: Any) -> tuple[bytes, dict[str, str]]:
    return gzip.compress(json.dumps(payload).encode()), {"Content-Encoding": "gzip"}
