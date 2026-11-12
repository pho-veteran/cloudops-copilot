#!/usr/bin/env python3
"""Generate sanitized E06 #68 wire/classification receipts from a loopback server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.transport import ModelClient

OUT = Path("docs/evidence/e06-r1-model-client")
TOKEN = "synthetic-test-token"
MESSAGES = [
    {"role": "system", "content": "You are a terse assistant."},
    {"role": "user", "content": "Reply with exactly: OK"},
]
TEXT = {
    "id": "chatcmpl-fixture",
    "model": "fixture-served-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
}


def base_profile(base_url: str) -> ModelProfile:
    return ModelProfile.model_validate(
        {
            "schema": "DiagnosisModelProfile/v1",
            "config_id": "e06-r1-receipt",
            "config_version": "1",
            "mode": "evaluation_only",
            "endpoint_label": "loopback-receipt",
            "base_url": base_url,
            "allow_insecure_loopback": True,
            "model": "fixture-model",
            "credential": {"kind": "env", "name": "DIAGNOSIS_MODEL_TOKEN"},
            "output_limit": {"param": "max_tokens", "value": 32},
            "authorization_id": "local-receipt",
            "budget_policy_id": "one-call",
        }
    )


def run_wire() -> dict:
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            captured.update(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {
                        key: ("<redacted>" if key.lower() == "authorization" else value)
                        for key, value in self.headers.items()
                    },
                    "body": json.loads(body),
                }
            )
            payload = json.dumps(TEXT).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = base_profile(f"http://127.0.0.1:{server.server_port}/v1")
        client = ModelClient(profile, environ={"DIAGNOSIS_MODEL_TOKEN": TOKEN})
        result = client.send(MESSAGES, before_send=lambda: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return {
        "schema": "E06ModelClientWire/v1",
        "request": captured,
        "result": {
            "kind": result.kind.value,
            "http_status": result.http_status,
            "text": result.text,
            "finish_reason": result.finish_reason,
            "usage": result.usage.__dict__ if result.usage else None,
            "usage_status": result.usage_status,
            "served_model": result.served_model,
            "error_code": result.error_code,
        },
        "proof_class": "Real loopback HTTP server; synthetic credential; Authorization redacted.",
    }


def _case(name: str, *, status: int, payload=None, headers=None) -> dict:
    profile = base_profile("http://127.0.0.1:18080/v1")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            json=payload if payload is not None else {},
            headers={"Content-Type": "application/json", **(headers or {})},
        )
    )
    result = ModelClient(
        profile,
        transport=transport,
        environ={"DIAGNOSIS_MODEL_TOKEN": TOKEN},
    ).send(MESSAGES, before_send=lambda: None)
    return {
        "case": name,
        "kind": result.kind.value,
        "http_status": result.http_status,
        "error_code": result.error_code,
        "retry_after_seconds": result.retry_after_seconds,
        "retry_after_raw": result.retry_after_raw,
    }


def run_matrix() -> dict:
    return {
        "schema": "E06ModelClientFailureMatrix/v1",
        "cases": [
            _case("text", status=200, payload=TEXT),
            _case(
                "refusal",
                status=200,
                payload={
                    "id": "r",
                    "model": "m",
                    "choices": [
                        {
                            "message": {"content": "", "refusal": "not allowed"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            ),
            _case(
                "truncated",
                status=200,
                payload={
                    "id": "t",
                    "model": "m",
                    "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
            _case("invalid_envelope", status=200, payload={"choices": []}),
            _case("auth_denied", status=401, payload={"error": {"message": "secret"}}),
            _case("rate_limited", status=429, headers={"Retry-After": "30"}),
            _case("upstream_error", status=503),
            _case("redirect", status=308, headers={"Location": "/v1/chat/completions"}),
            _case("request_rejected", status=404),
        ],
        "proof_class": "MockTransport classification matrix; no external network.",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "wire-capture.json").write_text(
        json.dumps(run_wire(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (OUT / "failure-matrix.json").write_text(
        json.dumps(run_matrix(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
