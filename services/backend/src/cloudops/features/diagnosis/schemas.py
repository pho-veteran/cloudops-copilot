"""Normalized, bounded outcome types for one model call."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Iterable, Mapping, Literal

USAGE_STATUS = Literal["observed", "unavailable", "invalid"]

# Bounds for opt-in provider rejection detail (trusted diagnostic tooling only).
PROVIDER_ERROR_MAX_BODY = 8192
PROVIDER_ERROR_MAX_MESSAGE = 512
_REDACTED = "[redacted]"
# A value is a double- or single-quoted string (escapes included), or a run of
# non-separator characters. A purely numeric value (a count) is not a secret.
_VALUE = r"(?:\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?|[^\s\"',;]+)"
_NOT_COUNT = r"(?!\d+(?:[\s,;.)]|$))"
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bbearer\s+" + _VALUE),
    re.compile(
        r"(?i)\b(?:api[_-]?key|key|token|secret|password|passwd|authorization|credential)s?\b"
        r"[\"']?\s*[:=]\s*" + _NOT_COUNT + _VALUE
    ),
    re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"),
    re.compile(r"[A-Za-z0-9_\-+/=]{32,}"),
    # Provider account identifiers (for example "organization org-…" in a 429 message).
    # The whole token is consumed; a digit separates IDs from words such as "user_error".
    re.compile(r"\b(?:org|proj|user)[-_](?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9][A-Za-z0-9_\-]{5,}"),
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:\-\[\]]{1,120}")


class CallKind(StrEnum):
    TEXT = "text"
    TRUNCATED = "truncated"
    REFUSAL = "refusal"
    TOOL_OUTPUT = "tool_output"
    UNSUPPORTED_FINISH = "unsupported_finish"
    UNSUPPORTED_SHAPE = "unsupported_shape"
    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    INVALID_ENCODING = "invalid_encoding"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    AUTH_DENIED = "auth_denied"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    REQUEST_REJECTED = "request_rejected"
    UNEXPECTED_REDIRECT = "unexpected_redirect"
    RESPONSE_TOO_LARGE = "response_too_large"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    CREDENTIAL_INVALID = "credential_invalid"
    INPUT_REJECTED = "input_rejected"
    CONFIG_INVALID = "config_invalid"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class CallIdentity:
    config_id: str
    config_version: str
    behavior_digest: str
    endpoint_label: str
    requested_model: str
    authorization_id: str
    budget_policy_id: str


@dataclass(frozen=True)
class ProviderError:
    """Bounded, redacted provider rejection detail for trusted diagnostic tooling.

    Product receipts never store it; it exists so an owner-authorized probe can
    explain a rejected request without retaining the raw body or any credential.
    """

    type: str | None
    code: str | None
    param: str | None
    message: str | None
    request_id: str | None
    # Bytes consumed from the stream (at most one chunk beyond the retained cap).
    body_bytes_read: int
    body_truncated: bool


@dataclass(frozen=True)
class ModelCall:
    kind: CallKind
    identity: CallIdentity | None
    text: str | None
    finish_reason: str | None
    usage: Usage | None
    usage_status: USAGE_STATUS
    request_id: str | None
    served_model: str | None
    http_status: int | None
    error_code: str | None
    retry_after_seconds: int | None
    retry_after_raw: str | None
    elapsed_ms: int
    response_bytes: int
    # Present only after a complete bounded envelope read; no raw body is retained.
    response_sha256: str | None = None
    # Opt-in only (ModelClient(capture_provider_error=True)); excluded from repr.
    provider_error: ProviderError | None = field(default=None, repr=False)

    def __post_init__(self):
        # Guard against accidentally putting provider text in a public code field.
        if self.error_code is not None and self.error_code != self.kind.value:
            raise ValueError("error_code must match the local CallKind")


def safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def redact_provider_text(value: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def _provider_identifier(value: object, secrets: Iterable[str]) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        return None
    return None if redact_provider_text(value, secrets) != value else value


def parse_provider_error(
    body: bytes,
    *,
    truncated: bool,
    headers: Mapping[str, str],
    consumed: int | None = None,
    secrets: Iterable[str] = (),
) -> ProviderError:
    """Extract only OpenAI-style error fields from a bounded rejection body."""
    secrets = tuple(secret for secret in secrets if secret)
    request_id = _provider_identifier(headers.get("x-request-id"), secrets)
    kind = code = param = message = None
    if not truncated:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            kind = _provider_identifier(error.get("type"), secrets)
            code = _provider_identifier(error.get("code"), secrets)
            param = _provider_identifier(error.get("param"), secrets)
            raw = error.get("message")
            if isinstance(raw, str):
                text = "".join(char if char.isprintable() else " " for char in raw)
                text = redact_provider_text(text, secrets).strip()
                message = text[:PROVIDER_ERROR_MAX_MESSAGE] or None
    return ProviderError(
        type=kind,
        code=code,
        param=param,
        message=message,
        request_id=request_id,
        body_bytes_read=len(body) if consumed is None else consumed,
        body_truncated=truncated,
    )


def parse_usage(raw: object) -> tuple[Usage | None, USAGE_STATUS]:
    if raw is None:
        return None, "unavailable"
    if not isinstance(raw, dict):
        return None, "invalid"
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    present = [key for key in keys if key in raw]
    if not present:
        return None, "invalid"
    values: dict[str, int | None] = {}
    for key in keys:
        if key not in raw:
            values[key] = None
            continue
        value = raw[key]
        if type(value) is not int or value < 0:
            return None, "invalid"
        values[key] = value
    return Usage(**values), "observed"


def parse_retry_after(headers: Mapping[str, str]) -> tuple[int | None, str | None]:
    raw_value = headers.get("retry-after")
    if raw_value is None:
        return None, None
    if not raw_value.isprintable():
        return None, None
    raw = raw_value[:64]
    if raw_value.isascii() and raw_value.isdecimal():
        digits = raw_value.lstrip("0") or "0"
        return min(int(digits), 86400) if len(digits) <= 5 else 86400, raw
    if len(raw_value) > 64:
        return None, raw
    try:
        when = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None, raw
    if when is None:
        return None, raw
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    return min(max(0, seconds), 86400), raw
