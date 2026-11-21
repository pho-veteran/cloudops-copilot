"""Deployment-owned Prometheus read configuration for the standard metric catalog."""

import base64
import binascii
import json
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

QUERY_IDS = (
    "request_rate",
    "error_ratio",
    "p95_latency",
    "memory_working_set",
    "memory_limit",
    "restarts",
)
PLACEHOLDERS = ("{{namespace}}", "{{workload_uid}}")
MAX_CONFIG_BYTES = 512 * 1024
_ENDPOINT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/-]+$")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4096)
    unit: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def scoped_template(self):
        placeholders = set(_PLACEHOLDER.findall(self.query))
        if placeholders - set(PLACEHOLDERS):
            raise ValueError("The query has an unsupported placeholder.")
        if not all(placeholder in placeholders for placeholder in PLACEHOLDERS):
            raise ValueError("The query must bind both namespace and workload UID.")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.unit):
            raise ValueError("The unit contains control characters.")
        return self


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment_uid_label: str = Field(default="deployment_uid", min_length=1, max_length=128)
    namespace_label: str = Field(default="namespace", min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_labels(self):
        for value in (self.deployment_uid_label, self.namespace_label):
            if not _LABEL.fullmatch(value):
                raise ValueError("Binding labels must be valid Prometheus label names.")
        if self.deployment_uid_label == self.namespace_label:
            raise ValueError("Binding labels must be distinct.")
        return self


def server_base(value: str) -> str:
    """Validate one fixed HTTPS origin with an optional deployment path prefix."""
    if (
        not value.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#@%")
    ):
        raise ValueError("The Prometheus server must be one fixed HTTPS URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("The Prometheus server must be one fixed HTTPS URL.")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("The Prometheus server port is invalid.") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The Prometheus server port is invalid.")
    path = parsed.path.rstrip("/")
    if path and (not _PATH.fullmatch(path) or ".." in path.split("/")):
        raise ValueError("The Prometheus server path is invalid.")
    return f"https://{parsed.netloc}{path}"


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema", min_length=1, max_length=64)
    endpoint_id: str = Field(min_length=1, max_length=64)
    producer_version: str = Field(min_length=1, max_length=64)
    server: str = Field(min_length=1, max_length=2048)
    ca_data: str = Field(min_length=1, max_length=65536)
    token: str | None = Field(default=None, max_length=4096)
    step_seconds: int = Field(default=30, ge=30, le=300)
    binding: Binding = Field(default_factory=Binding)
    queries: dict[str, QuerySpec]

    @model_validator(mode="after")
    def valid_config(self):
        if self.schema_ != "PrometheusReadConfig/v1":
            raise ValueError("The Prometheus configuration schema is unsupported.")
        if not _ENDPOINT.fullmatch(self.endpoint_id):
            raise ValueError("The Prometheus endpoint identifier is invalid.")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.producer_version):
            raise ValueError("The Prometheus producer version is invalid.")
        self.server = server_base(self.server)
        _ca_pem(self.ca_data)
        if self.token is not None and any(
            ord(char) < 32 or ord(char) == 127 for char in self.token
        ):
            raise ValueError("The Prometheus token is invalid.")
        if set(self.queries) != set(QUERY_IDS):
            raise ValueError(
                "The Prometheus configuration must bind every metric query exactly once."
            )
        return self

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=_ca_pem(self.ca_data).decode("ascii"))

    def query_url(self) -> str:
        return f"{self.server}/api/v1/query_range"


def _ca_pem(value: str) -> bytes:
    try:
        pem = base64.b64decode(value, validate=True)
        context = ssl.create_default_context(cadata=pem.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError, ValueError, ssl.SSLError):
        raise ValueError("The Prometheus CA is invalid.") from None
    if not context.get_ca_certs():
        raise ValueError("The Prometheus CA is invalid.")
    return pem


def escape_promql(value: str) -> str:
    """Escape one validated context value for a double-quoted PromQL string."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def substitute_query(template: str, namespace: str, workload_uid: str) -> str:
    if set(_PLACEHOLDER.findall(template)) - set(PLACEHOLDERS):
        raise ValueError("The query has an unsupported placeholder.")
    resolved = template.replace("{{namespace}}", escape_promql(namespace)).replace(
        "{{workload_uid}}", escape_promql(workload_uid)
    )
    if _PLACEHOLDER.search(resolved):
        raise ValueError("The query has an unsupported placeholder.")
    return resolved


def load_config(path: str | Path) -> PrometheusConfig:
    """Load one trusted file; failures never echo credentials or file contents."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError
        raw = json.loads(data.decode("utf-8"))
        return PrometheusConfig.model_validate(raw)
    except (OSError, ValueError, ValidationError, RecursionError):
        raise ValueError("Prometheus read configuration is invalid or unavailable.") from None
