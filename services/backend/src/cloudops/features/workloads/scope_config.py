"""Host-owned scope policy and bounded, read-only name resolution."""

import base64
import hashlib
import json
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.schemas import DevelopmentWorkload
from cloudops.shared.errors import DomainError
from cloudops.shared.cluster_identity import cluster_fingerprint, server_identity


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
    name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def dns_name(self):
        if any(
            not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", part)
            for part in self.name.split(".")
        ):
            raise ValueError("Use a valid Deployment DNS name.")
        return self


class ClusterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    fingerprint_algorithm: Literal["server-ca-sha256/v1"]
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class LiveScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2]
    cluster: ClusterBinding
    deployments: list[Target] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [(target.namespace, target.name) for target in self.deployments]
        if len(set(keys)) != len(keys):
            raise ValueError("Configured Deployment names must be unique.")
        return self


def load_scope(path: Path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            raise ValueError
        value = json.loads(raw)
    except (OSError, ValueError):
        raise DomainError(
            422, "scope_invalid", "The workload scope file is unavailable or invalid."
        ) from None
    if isinstance(value, list):
        scope = TypeAdapter(list[DevelopmentWorkload]).validate_python(value)
        keys = [(e.cluster, e.namespace, e.name, e.uid) for e in scope]
        if len(scope) > 100 or len(set(keys)) != len(keys):
            raise DomainError(
                422, "scope_invalid", "Configure at most 100 unique workload identities."
            )
    else:
        scope = LiveScope.model_validate(value)
    return scope, hashlib.sha256(raw).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Lookup:
    state: str
    uid: str | None = None
    reason: str | None = None


class ScopeReader:
    """One immutable reader snapshot; no generic destination or mutation interface."""

    def __init__(self, scope: LiveScope, path: Path):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            actual = cluster_fingerprint(config)
            token = config["token"]
            if not isinstance(token, str) or not token or any(ord(c) <= 32 for c in token):
                raise ValueError
            self.server = server_identity(config["server"])
            self.token = token
            self.opener = build_opener(
                ProxyHandler({}),
                NoRedirect(),
                HTTPSHandler(
                    context=ssl.create_default_context(
                        cadata=base64.b64decode(config["ca_data"], validate=True).decode("ascii")
                    )
                ),
            )
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise DomainError(
                422, "scope_invalid", "The trusted reader configuration is invalid."
            ) from None
        if actual != scope.cluster.expected_fingerprint:
            raise DomainError(
                409,
                "cluster_identity_mismatch",
                "Reader server/CA does not match the pinned scope policy.",
            )

    def lookup(self, target: Target, deadline: float) -> Lookup:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Lookup("unavailable", reason="budget_exhausted")
        request = Request(
            self.server + Kubernetes.path(target.namespace, target.name),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=min(8, remaining)) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise ValueError
                value = json.loads(payload)
                metadata = value["metadata"]
                uid = metadata["uid"]
                if (
                    value.get("kind") != "Deployment"
                    or value.get("apiVersion") != "apps/v1"
                    or metadata["name"] != target.name
                    or metadata["namespace"] != target.namespace
                    or not isinstance(uid, str)
                    or not 1 <= len(uid) <= 100
                ):
                    raise ValueError
                return Lookup("present", uid=uid)
        except HTTPError as error:
            if error.code == 404:
                try:
                    body = error.read(2_000_001)
                    value = json.loads(body) if len(body) <= 2_000_000 else None
                    details = value.get("details", {}) if isinstance(value, dict) else {}
                    if (
                        isinstance(value, dict)
                        and value.get("kind") == "Status"
                        and value.get("reason") == "NotFound"
                        and details.get("group") == "apps"
                        and details.get("kind") == "deployments"
                        and details.get("name") == target.name
                    ):
                        return Lookup("missing", reason="not_found")
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            return Lookup(
                "unavailable",
                reason="permission_denied" if error.code == 403 else "cluster_unavailable",
            )
        except (URLError, OSError, ValueError, KeyError, TypeError):
            return Lookup("unavailable", reason="cluster_unavailable")
