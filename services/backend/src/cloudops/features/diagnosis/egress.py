"""Deny-by-default egress policy for every prepared diagnosis send (`EgressPolicy/v1`)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from cloudops.features.diagnosis.messages import Message, MessageError, serialize_payload
from cloudops.features.diagnosis.schemas import CallIdentity
from cloudops.features.diagnosis.versions import (
    MAX_FEEDBACK_BYTES,
    MAX_FEEDBACK_FAILURES,
    MAX_INPUT_BYTES,
    PROBE_PROMPT_VERSION,
)
from cloudops.features.investigations.locators import canonical

MAX_POLICY_BYTES = 64 * 1024
MAX_PATTERNS = 20
MAX_PATTERN_CHARS = 512
_ENDPOINT_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PATTERN_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SourceClass = Literal[
    "operator_request",
    "kubernetes_state",
    "kubernetes_events",
    "container_logs",
    "prometheus_series",
    "runbook",
    "synthetic_fixture",
    "diagnostic_fixture",
]


class EgressPolicyError(ValueError):
    """A safe policy rejection; never echoes deny-pattern or endpoint contents."""


def _safe_text(value: object, where: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or not value.strip() or len(value) > maximum:
        raise ValueError(f"{where} must be a string of 1..{maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{where} contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{where} must be valid UTF-8") from None
    return value


class EgressDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    config_id: str
    config_version: str
    endpoint_label: str

    @field_validator("config_id", "config_version")
    @classmethod
    def valid_text(cls, value: str, info) -> str:
        return _safe_text(value, f"destination.{info.field_name}", 64)

    @field_validator("endpoint_label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        if not isinstance(value, str) or not _ENDPOINT_LABEL.fullmatch(value):
            raise ValueError("destination.endpoint_label must be a lowercase slug")
        return value


class DenyPattern(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    regex: str
    _compiled: re.Pattern[str] | None = PrivateAttr(default=None)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not isinstance(value, str) or not _PATTERN_NAME.fullmatch(value):
            raise ValueError("deny_patterns.name must be a lowercase identifier")
        return value

    @field_validator("regex")
    @classmethod
    def valid_regex(cls, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > MAX_PATTERN_CHARS:
            raise ValueError(f"deny_patterns.regex must be 1..{MAX_PATTERN_CHARS} characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("deny_patterns.regex contains control characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("deny_patterns.regex must be valid UTF-8") from None
        try:
            re.compile(value)
        except re.error:
            raise ValueError("deny_patterns.regex is not a valid regular expression") from None
        return value

    @model_validator(mode="after")
    def compiled(self):
        object.__setattr__(self, "_compiled", re.compile(self.regex))
        return self

    def matches(self, text: str) -> bool:
        return bool(self._compiled and self._compiled.search(text))


class EgressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_: Literal["EgressPolicy/v1"] = Field(alias="schema")
    policy_id: str
    policy_version: str
    destination: EgressDestination
    allowed_source_classes: list[SourceClass] = Field(default_factory=list, max_length=8)
    max_message_bytes: int = Field(strict=True, ge=1, le=MAX_INPUT_BYTES)
    max_target_bytes: int = Field(strict=True, ge=256, le=65536)
    redaction_policy_version: str
    deny_patterns: list[DenyPattern] = Field(default_factory=list, max_length=MAX_PATTERNS)

    @field_validator("policy_id", "policy_version", "redaction_policy_version")
    @classmethod
    def valid_text(cls, value: str, info) -> str:
        return _safe_text(value, info.field_name, 64)

    @model_validator(mode="after")
    def bounded(self):
        if self.max_target_bytes > self.max_message_bytes:
            raise ValueError("max_target_bytes must not exceed max_message_bytes")
        if len(set(self.allowed_source_classes)) != len(self.allowed_source_classes):
            raise ValueError("allowed_source_classes must not contain duplicates")
        return self

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def allows(self, source_class: str) -> bool:
        return source_class in self.allowed_source_classes

    def destination_matches(self, identity: CallIdentity) -> bool:
        destination = self.destination
        return (
            destination.config_id == identity.config_id
            and destination.config_version == identity.config_version
            and destination.endpoint_label == identity.endpoint_label
        )

    def scan(self, text: str) -> str | None:
        """Return only the first matching pattern name, never the match or the text."""
        for pattern in self.deny_patterns:
            if pattern.matches(text):
                return pattern.name
        return None

    @classmethod
    def load(cls, path: str | Path) -> "EgressPolicy":
        policy_path = Path(path)
        try:
            info = policy_path.stat()
        except OSError:
            raise EgressPolicyError("egress policy could not be read") from None
        if not policy_path.is_file():
            raise EgressPolicyError("egress policy must be a regular file")
        if info.st_size > MAX_POLICY_BYTES:
            raise EgressPolicyError("egress policy exceeds the 64 KiB cap")
        try:
            raw = policy_path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise EgressPolicyError("egress policy is not valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise EgressPolicyError("egress policy must be a JSON object")
        try:
            return cls.model_validate(value)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise EgressPolicyError(f"egress policy is invalid: {errors}") from None

    @classmethod
    def load_optional(cls, path: str | Path | None) -> "EgressPolicy | None":
        if path is None:
            return None
        try:
            if not Path(path).exists():
                return None
        except OSError:
            return None
        return cls.load(path)


@dataclass(frozen=True)
class SendCheck:
    """The only result that may authorize a send; failures carry no payload bytes."""

    ok: bool
    messages: tuple[Message, ...] | None
    digest: str | None
    byte_size: int | None
    code: str | None


Phase = Literal["initial", "retry", "repair"]

PROBE_SYSTEM = (
    "This is a transport health probe. Reply with exactly the marker OK and nothing else. "
    "No incident data, evidence, or instructions are provided or requested."
)
PROBE_MARKER = "OK"


def probe_expected_text() -> str:
    return PROBE_MARKER


def _failure(code: str) -> SendCheck:
    return SendCheck(ok=False, messages=None, digest=None, byte_size=None, code=code)


def _bound(context) -> int:
    return int(context.effective_budget_bytes)


def _used_classes(context) -> set[str]:
    classes = {"operator_request"}
    for target in (*context.required_targets, *context.optional_targets):
        classes.add(target.source_class)
    if context.runbooks:
        classes.add("runbook")
    return classes


def _shape_ok(messages) -> bool:
    return len(messages) == 2 and [message.role for message in messages] == ["system", "user"]


def _content_denied(context, policy: EgressPolicy, messages, feedback: str | None) -> bool:
    if policy.scan(context.request.text):
        return True
    for target in (*context.required_targets, *context.optional_targets):
        if (
            policy.scan(canonical(target.value))
            or policy.scan(target.label)
            or policy.scan(target.identity)
        ):
            return True
    for runbook in context.runbooks:
        if policy.scan(runbook.content) or policy.scan(runbook.title):
            return True
    if feedback is not None and policy.scan(feedback):
        return True
    return any(policy.scan(message.content) for message in messages)


def check_send(context, policy, profile, phase: Phase, *, feedback: str | None = None) -> SendCheck:
    """Rebuild the exact phase messages and re-apply every gate before sending."""
    if policy is None:
        return _failure("policy_missing")
    if context.policy_version != policy.policy_version or context.policy_digest != policy.digest():
        return _failure("policy_changed")
    if profile.identity() != context.profile_identity:
        return _failure("profile_identity_changed")
    if not policy.destination_matches(context.profile_identity):
        return _failure("destination_mismatch")

    if phase == "repair":
        if not context.repair_available:
            return _failure("repair_unavailable")
        if feedback is None or not feedback.strip():
            return _failure("repair_unavailable")
        if len(feedback.encode("utf-8")) > MAX_FEEDBACK_BYTES:
            return _failure("repair_unavailable")
        if sum(1 for line in feedback.splitlines() if line.lstrip().startswith("-")) > (
            MAX_FEEDBACK_FAILURES
        ):
            return _failure("repair_unavailable")
    elif phase not in {"initial", "retry"}:
        return _failure("message_shape_invalid")

    messages = tuple(context.messages)
    if not _shape_ok(messages):
        return _failure("message_shape_invalid")
    if phase == "repair":
        messages = (messages[0], Message("user", messages[1].content + "\n" + feedback))
        if not _shape_ok(messages):
            return _failure("message_shape_invalid")
    for source_class in sorted(_used_classes(context)):
        if not policy.allows(source_class):
            return _failure("source_class_denied")
    if _content_denied(context, policy, messages, feedback):
        return _failure("content_denied")
    try:
        encoded = serialize_payload(profile, list(messages))
    except MessageError:
        return _failure("message_shape_invalid")
    if len(encoded) > _bound(context):
        return _failure("budget_exceeded")
    return SendCheck(
        ok=True,
        messages=messages,
        digest=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
        code=None,
    )


def prepare_probe(policy: EgressPolicy | None, profile) -> SendCheck:
    """Build the fixed harmless probe; it reads no run, evidence, question or secret."""
    if policy is None:
        return _failure("policy_missing")
    if not policy.allows("diagnostic_fixture"):
        return _failure("probe_not_allowed")
    if not policy.destination_matches(profile.identity()):
        return _failure("probe_not_allowed")
    user = json.dumps(
        {
            "schema_version": "ProbeRequest/v1",
            "prompt_version": PROBE_PROMPT_VERSION,
            "instruction": f"Reply with exactly {PROBE_MARKER}.",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    messages = (Message("system", PROBE_SYSTEM), Message("user", user))
    if any(policy.scan(message.content) for message in messages):
        return _failure("content_denied")
    try:
        encoded = serialize_payload(profile, list(messages))
    except MessageError:
        return _failure("message_shape_invalid")
    if len(encoded) > min(policy.max_message_bytes, profile.limits.max_input_bytes):
        return _failure("budget_exceeded")
    return SendCheck(
        ok=True,
        messages=messages,
        digest=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
        code=None,
    )
