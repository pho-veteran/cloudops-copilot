"""One bounded, non-streaming OpenAI-compatible chat-completions call."""

from __future__ import annotations

import asyncio
import json
import hashlib
from dataclasses import replace
import os
import time
from collections.abc import Callable, Mapping

import httpx

from cloudops.features.diagnosis.messages import MessageError, prepare_payload
from cloudops.features.diagnosis.profile import Mode, ModelProfile, ProfileError
from cloudops.features.diagnosis.schemas import (
    PROVIDER_ERROR_MAX_BODY,
    CallIdentity,
    CallKind,
    ModelCall,
    ProviderError,
    parse_provider_error,
    parse_retry_after,
    parse_usage,
    safe_identifier,
)

_JSON_MEDIA = {"application/json"}
_ALLOWED_ENCODINGS = {"", "identity"}
_PROVIDER_ERROR_DEADLINE_MARGIN = 0.25


class _HookAbort(Exception):
    def __init__(self, original: BaseException) -> None:
        super().__init__("admission hook aborted the call")
        self.original = original


class ModelClient:
    """A provider-independent primitive; retry/repair orchestration belongs to #70."""

    def __init__(
        self,
        profile: ModelProfile | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        capture_provider_error: bool = False,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.environ = environ if environ is not None else os.environ
        # Trusted diagnostic tooling only: keep bounded, redacted rejection detail.
        self.capture_provider_error = capture_provider_error
        if transport is not None and profile is not None and profile.proxy is not None:
            raise ValueError("an injected transport cannot be combined with a profile proxy")

    @classmethod
    def from_config_path(
        cls,
        path: str | os.PathLike[str] | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ModelClient":
        return cls(
            ModelProfile.load_optional(path),
            transport=transport,
            environ=environ,
        )

    def send(
        self,
        messages,
        *,
        before_send: Callable[[], None],
    ) -> ModelCall:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.asend(messages, before_send=before_send))
        raise RuntimeError("Use await asend() inside an event loop.")

    async def asend(
        self,
        messages,
        *,
        before_send: Callable[[], None],
    ) -> ModelCall:
        started = time.monotonic()
        profile = self.profile
        if profile is None or profile.mode is Mode.DISABLED:
            return self._not_configured(profile, started)

        identity = profile.identity()
        credential, credential_error = self._credential(profile)
        if credential_error is not None:
            return self._failure(credential_error, identity, started)
        try:
            body = prepare_payload(profile, messages)
            url = profile.request_url()
            context = profile.ssl_context()
        except MessageError:
            return self._failure(CallKind.INPUT_REJECTED, identity, started)
        except ProfileError:
            return self._failure(CallKind.CONFIG_INVALID, identity, started)
        deadline = profile.limits.deadline_seconds
        phase = httpx.Timeout(
            connect=min(profile.limits.connect_timeout_seconds, deadline),
            read=min(profile.limits.read_timeout_seconds, deadline),
            write=min(profile.limits.read_timeout_seconds, deadline),
            pool=min(profile.limits.pool_timeout_seconds, deadline),
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"

        try:
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    verify=context,
                    proxy=profile.proxy_url(),
                    transport=self.transport,
                    timeout=phase,
                ) as client:
                    try:
                        before_send()
                    except BaseException as exc:
                        raise _HookAbort(exc) from exc
                    async with client.stream(
                        "POST", url, content=body, headers=headers
                    ) as response:
                        return await self._read(
                            response,
                            identity=identity,
                            started=started,
                            credential=credential,
                        )
        except _HookAbort as exc:
            raise exc.original
        except TimeoutError:
            return self._failure(CallKind.TIMEOUT, identity, started)
        except httpx.TimeoutException:
            return self._failure(CallKind.TIMEOUT, identity, started)
        except httpx.DecodingError:
            return self._failure(CallKind.INVALID_ENCODING, identity, started)
        except httpx.TransportError:
            return self._failure(CallKind.TRANSPORT_ERROR, identity, started)

    def _credential(self, profile: ModelProfile) -> tuple[str | None, CallKind | None]:
        if profile.credential is None:
            return None, None
        value = self.environ.get(profile.credential.name)
        if value is None or value == "":
            return None, CallKind.CREDENTIAL_UNAVAILABLE
        if (
            len(value) > 4096
            or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            return None, CallKind.CREDENTIAL_INVALID
        return value, None

    async def _read(
        self,
        response: httpx.Response,
        *,
        identity: CallIdentity,
        started: float,
        credential: str | None = None,
    ) -> ModelCall:
        status = response.status_code
        retry_seconds, retry_raw = parse_retry_after(response.headers)
        detail = (
            await self._provider_error(response, credential, started)
            if status >= 400 and self.capture_provider_error
            else None
        )
        if 300 <= status < 400:
            return self._failure(
                CallKind.UNEXPECTED_REDIRECT,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
            )
        if status in {401, 403}:
            return self._failure(
                CallKind.AUTH_DENIED,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
                provider_error=detail,
            )
        if status == 429:
            return self._failure(
                CallKind.RATE_LIMITED,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
                provider_error=detail,
            )
        if status >= 500:
            return self._failure(
                CallKind.UPSTREAM_ERROR,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
                provider_error=detail,
            )
        if status >= 400:
            return self._failure(
                CallKind.REQUEST_REJECTED,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
                provider_error=detail,
            )
        if not 200 <= status < 300:
            return self._failure(
                CallKind.INVALID_ENVELOPE,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
            )

        encoding = response.headers.get("content-encoding", "").lower()
        if encoding not in _ALLOWED_ENCODINGS:
            return self._failure(
                CallKind.INVALID_ENCODING,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
            )
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in _JSON_MEDIA and not media.endswith("+json"):
            return self._failure(
                CallKind.INVALID_CONTENT_TYPE,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
            )
        length = response.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                declared = None
            if declared is not None and declared > self._max_response():
                return self._failure(
                    CallKind.RESPONSE_TOO_LARGE,
                    identity,
                    started,
                    status=status,
                    retry=(retry_seconds, retry_raw),
                )
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                received = len(body) + len(chunk)
                if received > self._max_response():
                    return self._failure(
                        CallKind.RESPONSE_TOO_LARGE,
                        identity,
                        started,
                        status=status,
                        retry=(retry_seconds, retry_raw),
                        response_bytes=received,
                    )
                body.extend(chunk)
        except httpx.DecodingError:
            return self._failure(
                CallKind.INVALID_ENCODING,
                identity,
                started,
                status=status,
                retry=(retry_seconds, retry_raw),
            )
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            return replace(
                self._failure(
                    CallKind.INVALID_ENVELOPE,
                    identity,
                    started,
                    status=status,
                    retry=(retry_seconds, retry_raw),
                    response_bytes=len(body),
                ),
                response_sha256=hashlib.sha256(body).hexdigest(),
            )
        return replace(
            self._normalize(
                value,
                identity=identity,
                status=status,
                started=started,
                retry=(retry_seconds, retry_raw),
                response_bytes=len(body),
            ),
            response_sha256=hashlib.sha256(body).hexdigest(),
        )

    def _normalize(
        self,
        payload: object,
        *,
        identity: CallIdentity,
        status: int,
        started: float,
        retry: tuple[int | None, str | None],
        response_bytes: int,
    ) -> ModelCall:
        if not isinstance(payload, dict):
            return self._failure(
                CallKind.INVALID_ENVELOPE,
                identity,
                started,
                status=status,
                retry=retry,
                response_bytes=response_bytes,
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return self._failure(
                CallKind.INVALID_ENVELOPE,
                identity,
                started,
                status=status,
                retry=retry,
                response_bytes=response_bytes,
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return self._failure(
                CallKind.INVALID_ENVELOPE,
                identity,
                started,
                status=status,
                retry=retry,
                response_bytes=response_bytes,
            )
        finish = choice.get("finish_reason")
        content = message.get("content")
        refusal = message.get("refusal")
        tool_calls = message.get("tool_calls")
        usage, usage_status = parse_usage(payload.get("usage"))
        request_id = safe_identifier(payload.get("id"))
        served_model = safe_identifier(payload.get("model"))

        kind: CallKind
        text: str | None = None
        if isinstance(refusal, str) and refusal.strip():
            kind = CallKind.REFUSAL
            text = content if isinstance(content, str) else None
        elif finish == "content_filter":
            kind = CallKind.REFUSAL
            text = content if isinstance(content, str) else None
        elif isinstance(tool_calls, list) and tool_calls:
            kind = CallKind.TOOL_OUTPUT
        elif finish == "tool_calls":
            kind = CallKind.TOOL_OUTPUT
        elif finish == "length":
            if isinstance(content, str):
                kind, text = CallKind.TRUNCATED, content
            else:
                kind = CallKind.UNSUPPORTED_SHAPE
        elif finish == "stop":
            if isinstance(content, str):
                kind, text = CallKind.TEXT, content
            else:
                kind = CallKind.UNSUPPORTED_SHAPE
        elif not isinstance(finish, str):
            kind = CallKind.UNSUPPORTED_FINISH
        else:
            kind = CallKind.UNSUPPORTED_FINISH
        return ModelCall(
            kind=kind,
            identity=identity,
            text=text,
            finish_reason=finish if isinstance(finish, str) else None,
            usage=usage,
            usage_status=usage_status,
            request_id=request_id,
            served_model=served_model,
            http_status=status,
            error_code=None
            if kind in {CallKind.TEXT, CallKind.TRUNCATED, CallKind.REFUSAL, CallKind.TOOL_OUTPUT}
            else kind.value,
            retry_after_seconds=retry[0],
            retry_after_raw=retry[1],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            response_bytes=response_bytes,
        )

    def _not_configured(self, profile: ModelProfile | None, started: float) -> ModelCall:
        return ModelCall(
            kind=CallKind.NOT_CONFIGURED,
            identity=profile.identity() if profile is not None else None,
            text=None,
            finish_reason=None,
            usage=None,
            usage_status="unavailable",
            request_id=None,
            served_model=None,
            http_status=None,
            error_code=CallKind.NOT_CONFIGURED.value,
            retry_after_seconds=None,
            retry_after_raw=None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            response_bytes=0,
        )

    def _failure(
        self,
        kind: CallKind,
        identity: CallIdentity,
        started: float,
        *,
        status: int | None = None,
        retry: tuple[int | None, str | None] = (None, None),
        response_bytes: int = 0,
        provider_error: ProviderError | None = None,
    ) -> ModelCall:
        return ModelCall(
            kind=kind,
            identity=identity,
            text=None,
            finish_reason=None,
            usage=None,
            usage_status="unavailable",
            request_id=None,
            served_model=None,
            http_status=status,
            error_code=kind.value,
            retry_after_seconds=retry[0],
            retry_after_raw=retry[1],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            response_bytes=response_bytes,
            provider_error=provider_error,
        )

    async def _provider_error(
        self, response: httpx.Response, credential: str | None, started: float
    ) -> ProviderError:
        """Keep at most PROVIDER_ERROR_MAX_BODY identity-encoded bytes of a rejection.

        Reading stops at the first chunk that reaches the cap, so at most one transport
        chunk beyond it is consumed. A body that reaches the cap is treated as truncated
        because its end was not observed. The read gets only the time left before the
        call deadline, minus a margin, so a stalled body keeps the rejection status.
        """
        assert self.profile is not None
        body = bytearray()
        consumed = 0
        truncated = False
        left = started + self.profile.limits.deadline_seconds - time.monotonic()
        # Leave a margin before the call deadline, scaled down when little time is left,
        # so an immediately available body is still read late in the call.
        remaining = left - min(_PROVIDER_ERROR_DEADLINE_MARGIN, left / 4)
        if response.headers.get("content-encoding", "").lower() not in _ALLOWED_ENCODINGS:
            truncated = True
        elif remaining <= 0.01:
            truncated = True
        else:
            try:
                async with asyncio.timeout(remaining):
                    async for chunk in response.aiter_bytes():
                        consumed += len(chunk)
                        body.extend(chunk[: PROVIDER_ERROR_MAX_BODY - len(body)])
                        if consumed >= PROVIDER_ERROR_MAX_BODY:
                            truncated = True
                            break
            except (TimeoutError, httpx.TransportError, httpx.StreamError, httpx.DecodingError):
                truncated = True
        return parse_provider_error(
            bytes(body),
            truncated=truncated,
            consumed=consumed,
            headers=response.headers,
            secrets=(credential,) if credential else (),
        )

    def _max_response(self) -> int:
        assert self.profile is not None
        return self.profile.limits.max_response_bytes
