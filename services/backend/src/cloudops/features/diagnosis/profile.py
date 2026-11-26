"""Validated, secret-free deployment profile for one OpenAI-compatible model endpoint."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from cloudops.features.diagnosis.schemas import CallIdentity
from cloudops.features.diagnosis.versions import MAX_INPUT_BYTES

SCHEMA = "DiagnosisModelProfile/v1"
MAX_PROFILE_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_ENDPOINT_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PATH_CHARS = re.compile(r"^[A-Za-z0-9._~/-]*$")
_PROXY_PATH = re.compile(r"^/?$")


class Mode(StrEnum):
    DISABLED = "disabled"
    EVALUATION_ONLY = "evaluation_only"
    OPERATIONAL = "operational"


MAX_CALL_SECONDS = 120


class OutputLimitParam(StrEnum):
    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"
    OMIT = "omit"


class ProfileError(ValueError):
    """A safe profile rejection; never echoes credentials or endpoint contents."""


def _safe_text(value: Any, where: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or not value.strip() or len(value) > maximum:
        raise ValueError(f"{where} must be a string of 1..{maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{where} contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{where} must be valid UTF-8") from None
    return value


def _safe_optional_text(value: Any, where: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _safe_text(value, where, maximum)


def validate_base_url(value: str, allow_loopback_http: bool) -> str:
    """Return one canonical API root; append ``chat/completions`` elsewhere."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("base_url must be a URL of 1..2048 characters")
    if not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("base_url must be printable ASCII without whitespace")
    if any(char in value for char in "\\?#@%"):
        raise ValueError("base_url must not contain credentials, query, fragment or escapes")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("base_url is not a valid URL") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base_url port is invalid")
    host = parsed.hostname.lower()
    if scheme == "http":
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if not allow_loopback_http or not loopback:
            raise ValueError("base_url may use HTTP only for an explicitly allowed loopback host")
    path = parsed.path.rstrip("/")
    if "//" in parsed.path:
        raise ValueError("base_url path contains an empty or relative segment")
    if path and not _PATH_CHARS.fullmatch(path):
        raise ValueError("base_url path contains unsupported characters")
    segments = path.split("/")[1:] if path else []
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("base_url path contains an empty or relative segment")
    lowered = path.lower().rstrip("/")
    if lowered.endswith("/chat/completions") or lowered == "/chat/completions":
        raise ValueError("base_url must be the API root, not a chat/completions URL")
    return urlunsplit((scheme, parsed.netloc, path.rstrip("/"), "", ""))


def validate_proxy_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("proxy.url must be a URL of 1..2048 characters")
    if not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("proxy.url must be printable ASCII without whitespace")
    if any(char in value for char in "\\?#@%"):
        raise ValueError("proxy.url must not contain credentials, query, fragment or escapes")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("proxy.url is not a valid URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("proxy.url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy.url must not contain credentials")
    if parsed.query or parsed.fragment or not _PROXY_PATH.fullmatch(parsed.path):
        raise ValueError("proxy.url must not contain a path, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("proxy.url port is invalid")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


class CredentialRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["env"] = "env"
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("credential.name must be an uppercase environment variable name")
        return value


class TlsRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ca_file: str | None = None

    @field_validator("ca_file")
    @classmethod
    def valid_path(cls, value: str | None) -> str | None:
        return _safe_optional_text(value, "tls.ca_file", 2048)


class ProxyRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return validate_proxy_url(value)


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_input_bytes: int = Field(default=65536, strict=True)
    max_response_bytes: int = Field(default=262144, strict=True)
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 2.0
    deadline_seconds: float = 30.0

    @field_validator(
        "max_input_bytes",
        "max_response_bytes",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "pool_timeout_seconds",
        "deadline_seconds",
        mode="before",
    )
    @classmethod
    def reject_bool(cls, value: Any, info) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be a number")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if not 1 <= self.max_input_bytes <= MAX_INPUT_BYTES:
            raise ValueError(f"max_input_bytes must be 1..{MAX_INPUT_BYTES}")
        if not 1 <= self.max_response_bytes <= 262144:
            raise ValueError("max_response_bytes must be 1..262144")
        if not 0.1 <= self.connect_timeout_seconds <= 10:
            raise ValueError("connect_timeout_seconds must be 0.1..10")
        # Reasoning models can take well over 30 seconds for a bounded non-streaming
        # answer. Three calls at the 120-second cap still fit the 900-second analysis
        # deadline, and the worker renews its lease throughout a call.
        if not 0.1 <= self.read_timeout_seconds <= MAX_CALL_SECONDS:
            raise ValueError(f"read_timeout_seconds must be 0.1..{MAX_CALL_SECONDS}")
        if not 0.1 <= self.pool_timeout_seconds <= 10:
            raise ValueError("pool_timeout_seconds must be 0.1..10")
        if not 1 <= self.deadline_seconds <= MAX_CALL_SECONDS:
            raise ValueError(f"deadline_seconds must be 1..{MAX_CALL_SECONDS}")
        return self


class OutputLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    param: OutputLimitParam
    value: int | None = None
    omit_policy_id: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool) or (value is not None and not isinstance(value, int)):
            raise ValueError("output_limit.value must be an integer")
        return value

    @field_validator("omit_policy_id")
    @classmethod
    def valid_policy(cls, value: str | None) -> str | None:
        return _safe_optional_text(value, "output_limit.omit_policy_id", 128)

    @model_validator(mode="after")
    def valid_choice(self):
        if self.param is OutputLimitParam.OMIT:
            if self.value is not None:
                raise ValueError("output_limit.value must be null when param is omit")
            if not self.omit_policy_id:
                raise ValueError("output_limit.omit_policy_id is required when param is omit")
        else:
            if self.value is None or not 1 <= self.value <= 384000:
                raise ValueError("output_limit.value must be 1..384000")
            if self.omit_policy_id is not None:
                raise ValueError("output_limit.omit_policy_id must be null unless param is omit")
        return self


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
    schema_: Literal["DiagnosisModelProfile/v1"] = Field(alias="schema")
    config_id: str
    config_version: str
    mode: Mode = Mode.DISABLED
    endpoint_label: str | None = None
    base_url: str | None = None
    allow_insecure_loopback: bool = False
    model: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    credential: CredentialRef | None = None
    tls: TlsRef = Field(default_factory=TlsRef)
    proxy: ProxyRef | None = None
    limits: Limits = Field(default_factory=Limits)
    output_limit: OutputLimit | None = None
    authorization_id: str | None = None
    budget_policy_id: str | None = None
    _base_dir: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_profile(self):
        _safe_text(self.config_id, "config_id", 64)
        _safe_text(self.config_version, "config_version", 64)
        if self.endpoint_label is not None and not _ENDPOINT_LABEL.fullmatch(self.endpoint_label):
            raise ValueError("endpoint_label must be a lowercase slug")
        if self.model is not None:
            _safe_text(self.model, "model", 200)
        if self.base_url is not None:
            object.__setattr__(
                self,
                "base_url",
                validate_base_url(self.base_url, self.allow_insecure_loopback),
            )
        if self.authorization_id is not None:
            _safe_text(self.authorization_id, "authorization_id", 128)
        if self.budget_policy_id is not None:
            _safe_text(self.budget_policy_id, "budget_policy_id", 128)
        if self.mode is not Mode.DISABLED:
            for field in (
                "endpoint_label",
                "base_url",
                "model",
                "authorization_id",
                "budget_policy_id",
            ):
                if getattr(self, field) is None:
                    raise ValueError(f"{field} is required when mode is not disabled")
            if self.output_limit is None:
                raise ValueError("output_limit is required when mode is not disabled")
        return self

    def request_url(self) -> str:
        if self.base_url is None:
            raise ProfileError("base_url is not configured")
        parsed = urlsplit(self.base_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path + "/chat/completions", "", ""))

    def ssl_context(self) -> ssl.SSLContext:
        if self.tls.ca_file is None:
            return ssl.create_default_context()
        path = Path(self.tls.ca_file)
        if not path.is_absolute() and self._base_dir is not None:
            path = self._base_dir / path
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise ProfileError("tls.ca_file is not a usable PEM certificate") from exc

    def proxy_url(self) -> str | None:
        return self.proxy.url if self.proxy else None

    def identity(self) -> CallIdentity:
        return CallIdentity(
            config_id=self.config_id,
            config_version=self.config_version,
            behavior_digest=self.behavior_digest(),
            endpoint_label=self.endpoint_label or "",
            requested_model=self.model or "",
            authorization_id=self.authorization_id or "",
            budget_policy_id=self.budget_policy_id or "",
        )

    def behavior_digest(self) -> str:
        payload = {
            "base_url": self.base_url,
            "model": self.model,
            "limits": self.limits.model_dump(mode="json"),
            "output_limit": self.output_limit.model_dump(mode="json")
            if self.output_limit
            else None,
            "tls": {"ca_file": self.tls.ca_file},
            "proxy": self.proxy.url if self.proxy else None,
            "credential_name": self.credential.name if self.credential else None,
            "authorization_id": self.authorization_id,
            "budget_policy_id": self.budget_policy_id,
        }
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "ModelProfile":
        profile_path = Path(path)
        try:
            info = profile_path.stat()
        except OSError:
            raise ProfileError("model profile could not be read") from None
        if not profile_path.is_file():
            raise ProfileError("model profile must be a regular file")
        if info.st_size > MAX_PROFILE_BYTES:
            raise ProfileError("model profile exceeds the 64 KiB cap")
        try:
            raw = profile_path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise ProfileError("model profile is not valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise ProfileError("model profile must be a JSON object")
        try:
            profile = cls.model_validate(value)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise ProfileError(f"model profile is invalid: {errors}") from None
        profile._base_dir = profile_path.resolve().parent
        if profile.tls.ca_file is not None:
            profile.ssl_context()
        return profile

    @classmethod
    def load_optional(cls, path: str | Path | None) -> "ModelProfile | None":
        if path is None:
            return None
        try:
            if not Path(path).exists():
                return None
        except OSError:
            return None
        return cls.load(path)
