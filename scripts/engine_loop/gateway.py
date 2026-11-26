"""Model calls under the engine-loop call policy.

Every request goes through the product `ModelClient`. Responses are cached by the
SHA-256 of the exact serialized request body (model, parameters and messages), so a
replay re-sends only requests whose digest changed. At most three requests are in
flight. Authentication, payment, quota and rate-limit failures, or three consecutive
5xx/timeouts, stop the loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from cloudops.features.diagnosis.messages import Message, serialize_payload
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.schemas import CallKind
from cloudops.features.diagnosis.transport import ModelClient

MODEL = "cmd/deepseek/deepseek-v4.1-flash"
BASE_URL = "http://127.0.0.1:20128/v1"
CREDENTIAL_ENV = "ENGINE_MODEL_API_KEY"
MAX_TOKENS = 8000
MAX_CONCURRENCY = 3
CACHEABLE = {CallKind.TEXT, CallKind.TRUNCATED, CallKind.REFUSAL}
STOP_KINDS = {CallKind.AUTH_DENIED, CallKind.RATE_LIMITED}
STOP_STATUS = {401, 402, 403, 429}
TRANSIENT_KINDS = {CallKind.UPSTREAM_ERROR, CallKind.TIMEOUT, CallKind.TRANSPORT_ERROR}
QUOTA_MARKERS = ("quota", "credit", "insufficient_balance", "billing", "payment")


class PolicyStop(RuntimeError):
    """The endpoint returned a stop condition; no further calls may be sent."""


def profile(*, max_tokens: int = MAX_TOKENS, max_input_bytes: int = 65536) -> ModelProfile:
    return ModelProfile.model_validate(
        {
            "schema": "DiagnosisModelProfile/v1",
            "config_id": "engine-loop-dev",
            "config_version": "1",
            "mode": "evaluation_only",
            "endpoint_label": "local-proxy",
            "base_url": BASE_URL,
            "allow_insecure_loopback": True,
            "model": MODEL,
            "credential": {"kind": "env", "name": CREDENTIAL_ENV},
            "limits": {
                "max_input_bytes": max_input_bytes,
                "max_response_bytes": 262144,
                "connect_timeout_seconds": 3,
                "read_timeout_seconds": 120,
                "pool_timeout_seconds": 2,
                "deadline_seconds": 120,
            },
            "output_limit": {"param": "max_tokens", "value": max_tokens},
            "authorization_id": "engine-loop-dev",
            "budget_policy_id": "engine-loop-call-policy-v1",
        }
    )


def check_environment(environ: Mapping[str, str]) -> None:
    """Refuse any model or route other than the one the loop is allowed to use."""
    if environ.get("ENGINE_MODEL_NAME") != MODEL:
        raise PolicyStop("ENGINE_MODEL_NAME is not the allowed model")
    base = (environ.get("ENGINE_MODEL_BASE_URL") or "").replace("localhost", "127.0.0.1")
    if base.rstrip("/") != BASE_URL:
        raise PolicyStop("ENGINE_MODEL_BASE_URL is not the allowed route")
    if not environ.get(CREDENTIAL_ENV):
        raise PolicyStop("model credential variable is not set")


def request_digest(model_profile: ModelProfile, messages: list[Message]) -> str:
    return hashlib.sha256(serialize_payload(model_profile, messages)).hexdigest()


class _Recording(httpx.AsyncBaseTransport):
    """Keeps the response envelope so usage details (cached, reasoning) can be recorded."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.status: int | None = None
        self.body: bytes | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        self.status = response.status_code
        self.body = body
        return httpx.Response(
            response.status_code, headers=response.headers, content=body, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


def _usage(body: bytes | None) -> dict:
    try:
        value = json.loads(body or b"")
    except (ValueError, UnicodeDecodeError):
        return {}
    usage = value.get("usage") if isinstance(value, dict) else None
    if not isinstance(usage, dict):
        return {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": prompt_details.get("cached_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
    }


@dataclass
class Gateway:
    cache_dir: Path
    model_profile: ModelProfile
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    concurrency: int = MAX_CONCURRENCY
    calls_log: Path | None = None
    consecutive_transient: int = 0
    stopped: str | None = None
    sent: int = 0
    cache_hits: int = 0

    def __post_init__(self):
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError("at most three requests may be in flight")
        if self.model_profile.output_limit.value > MAX_TOKENS:
            raise ValueError("max_tokens is capped at 8000")
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def _cache_path(self, digest: str) -> Path:
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _log(self, record: dict) -> None:
        if self.calls_log is None:
            return
        self.calls_log.parent.mkdir(parents=True, exist_ok=True)
        with self.calls_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    async def call(self, messages: list[Message], *, case_id: str, phase: str) -> dict:
        if not messages or messages[0].role != "system":
            raise ValueError("every request must start with a system message")
        digest = request_digest(self.model_profile, messages)
        path = self._cache_path(digest)
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            self.cache_hits += 1
            result = {**record, "cached": True}
            self._log({"case_id": case_id, "phase": phase, **_public(result)})
            return result
        async with self._semaphore:
            if self.stopped:
                raise PolicyStop(self.stopped)
            transport = _Recording(
                self.transport_factory()
                if self.transport_factory
                else httpx.AsyncHTTPTransport(retries=0)
            )
            client = ModelClient(
                self.model_profile,
                transport=transport,
                environ=self.environ,
                capture_provider_error=True,
            )
            started = time.monotonic()
            call = await client.asend(messages, before_send=lambda: None)
            self.sent += 1
        record = {
            "request_digest": digest,
            "kind": call.kind.value,
            "http_status": call.http_status,
            "served_model": call.served_model,
            "finish_reason": call.finish_reason,
            "elapsed_ms": call.elapsed_ms or int((time.monotonic() - started) * 1000),
            "usage": _usage(transport.body),
            "text": call.text,
            "provider_message": (
                call.provider_error.message if call.provider_error is not None else None
            ),
        }
        self._check_stop(call.kind, call.http_status, record["provider_message"])
        if call.kind in CACHEABLE:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        result = {**record, "cached": False}
        self._log({"case_id": case_id, "phase": phase, **_public(result)})
        return result

    def _check_stop(self, kind: CallKind, status: int | None, message: str | None) -> None:
        text = (message or "").lower()
        if (
            kind in STOP_KINDS
            or status in STOP_STATUS
            or any(marker in text for marker in QUOTA_MARKERS)
        ):
            self.stopped = f"stop condition: kind={kind.value} status={status}"
        elif kind in TRANSIENT_KINDS:
            self.consecutive_transient += 1
            if self.consecutive_transient >= 3:
                self.stopped = "stop condition: three consecutive 5xx or timeouts"
        else:
            self.consecutive_transient = 0
        if self.stopped:
            raise PolicyStop(self.stopped)


def _public(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "text"}


async def probe(environ: Mapping[str, str] | None = None, transport_factory=None) -> dict:
    """The fixed session-start health probe: system message, exact reply, 16 tokens."""
    environ = os.environ if environ is None else environ
    check_environment(environ)
    model_profile = profile(max_tokens=16)
    transport = _Recording(
        transport_factory() if transport_factory else httpx.AsyncHTTPTransport(retries=0)
    )
    client = ModelClient(model_profile, transport=transport, environ=environ)
    call = await client.asend(
        [Message("system", "You are a health probe."), Message("user", "Reply with exactly: OK")],
        before_send=lambda: None,
    )
    return {
        "kind": call.kind.value,
        "http_status": call.http_status,
        "served_model": call.served_model,
        "text": call.text,
        "elapsed_ms": call.elapsed_ms,
        "usage": _usage(transport.body),
        "healthy": call.kind is CallKind.TEXT and (call.text or "").strip() == "OK",
    }
