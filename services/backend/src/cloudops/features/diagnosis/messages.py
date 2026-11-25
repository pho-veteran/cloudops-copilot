"""Primitive role/content messages and bounded JSON request serialization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from cloudops.features.diagnosis.profile import ModelProfile, OutputLimitParam

Role = Literal["system", "user"]
_ALLOWED_ROLES = {"system", "user"}
_ALLOWED_CONTROL = {"\n", "\r", "\t"}


class MessageError(ValueError):
    """A safe message-shape rejection; no network call has been made."""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


def _message(value: object, index: int) -> Message:
    where = f"messages[{index}]"
    if isinstance(value, Message):
        role, content = value.role, value.content
    elif isinstance(value, Mapping):
        if set(value) != {"role", "content"}:
            raise MessageError(f"{where} must contain only role and content")
        role, content = value["role"], value["content"]
    else:
        raise MessageError(f"{where} must be a Message or an object")
    if not isinstance(role, str) or role not in _ALLOWED_ROLES:
        raise MessageError(f"{where}.role must be system or user")
    if not isinstance(content, str):
        raise MessageError(f"{where}.content must be a string")
    for char in content:
        if char in _ALLOWED_CONTROL:
            continue
        if ord(char) < 32 or ord(char) == 127:
            raise MessageError(f"{where}.content contains control characters")
    return Message(role=role, content=content)


def _normalize(messages: Sequence[Message | Mapping[str, object]]) -> tuple[Message, ...]:
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise MessageError("messages must be a sequence")
    if not 1 <= len(messages) <= 2:
        raise MessageError("messages must contain one user message and at most one system message")
    normalized = tuple(_message(value, index) for index, value in enumerate(messages))
    systems = [message for message in normalized if message.role == "system"]
    users = [message for message in normalized if message.role == "user"]
    if len(systems) > 1:
        raise MessageError("messages may contain at most one system message")
    if not users:
        raise MessageError("messages must contain at least one user message")
    if systems and normalized[0].role != "system":
        raise MessageError("the system message must be first")
    return normalized


def serialize_payload(
    profile: ModelProfile,
    messages: Sequence[Message | Mapping[str, object]],
) -> bytes:
    """Encode the exact request body the transport would send; the only size definition."""
    if profile.model is None or profile.output_limit is None:
        raise MessageError("the model profile is not sendable")
    normalized = _normalize(messages)
    body: dict[str, object] = {
        "model": profile.model,
        "messages": [{"role": message.role, "content": message.content} for message in normalized],
        "stream": False,
    }
    if profile.output_limit.param is not OutputLimitParam.OMIT:
        body[profile.output_limit.param.value] = profile.output_limit.value
    if profile.reasoning_effort is not None:
        body["reasoning_effort"] = profile.reasoning_effort
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise MessageError("serialized model input contains an invalid Unicode sequence") from None


def prepare_payload(
    profile: ModelProfile,
    messages: Sequence[Message | Mapping[str, object]],
) -> bytes:
    encoded = serialize_payload(profile, messages)
    if len(encoded) > profile.limits.max_input_bytes:
        raise MessageError("serialized model input exceeds max_input_bytes")
    return encoded
