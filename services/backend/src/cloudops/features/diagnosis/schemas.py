"""Normalized, bounded outcome types for one model call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Mapping, Literal

USAGE_STATUS = Literal["observed", "unavailable", "invalid"]


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

    def __post_init__(self):
        # Guard against accidentally putting provider text in a public code field.
        if self.error_code is not None and self.error_code not in {kind.value for kind in CallKind}:
            raise ValueError("error_code must be a local CallKind value")


def safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


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
    raw = raw_value[:64]
    if not raw.isprintable():
        return None, None
    try:
        return min(max(0, int(raw)), 86400), raw
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None, raw
    if when is None:
        return None, raw
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    return min(max(0, seconds), 86400), raw
