"""Bounded, read-only resolution of alert labels to one configured live Deployment identity.

Labels only select candidates. Ownership comes from the pinned reader: a direct Deployment
read must return the configured UID, and a Pod must be controlled by a ReplicaSet that is
controlled by that UID. Destinations are fixed API paths; redirects are never followed.
"""

import base64
import json
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudops.features.workloads.models import ScopeState, Workload
from cloudops.features.workloads.scope_config import cluster_fingerprint, server_identity

LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
MAX_REQUESTS = 20
DEADLINE_SECONDS = 10.0
RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True)
class Resolved:
    workload_id: UUID
    uid: str
    namespace: str
    name: str


@dataclass(frozen=True)
class Refused:
    reason: str
    retry: bool = False


@dataclass(frozen=True)
class ScopeSnapshot:
    pinned: bool
    fingerprint: str | None
    # (namespace, name) -> (workload id, uid) for active live identities of the cluster.
    targets: dict[tuple[str, str], tuple[UUID, str]] = field(default_factory=dict)


def scope_snapshot(db: Session, cluster: str) -> ScopeSnapshot:
    state = db.get(ScopeState, 1)
    if not state or state.mode != "v2" or state.cluster_key != cluster or not state.fingerprint:
        return ScopeSnapshot(False, None)
    rows = db.execute(
        select(Workload.id, Workload.namespace, Workload.name, Workload.uid).where(
            Workload.active,
            Workload.source == "live_kubernetes",
            Workload.cluster == cluster,
        )
    ).all()
    return ScopeSnapshot(
        True, state.fingerprint, {(r.namespace, r.name): (r.id, r.uid) for r in rows}
    )


def dns_name(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 253
        and all(LABEL.fullmatch(part) for part in value.split("."))
    )


class _Missing(Exception):
    pass


class _Retry(Exception):
    def __init__(self, reason="cluster_unavailable"):
        self.reason = reason


class IdentityResolver:
    """One notification's resolver: shared request budget and per-target cache."""

    def __init__(self, cluster: str, snapshot: ScopeSnapshot, *, transport=None):
        self.cluster = cluster
        self.snapshot = snapshot
        self.transport = transport
        self.client: httpx.Client | None = None
        self.requests = 0
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.cache: dict[tuple[str, str, str], Resolved | Refused] = {}

    def close(self):
        if self.client:
            self.client.close()

    def resolve(self, labels: dict[str, str]) -> Resolved | Refused:
        if not self.snapshot.pinned:
            return Refused("scope_unpinned", retry=True)
        namespace = labels.get("namespace")
        deployment, pod = labels.get("deployment"), labels.get("pod")
        if not namespace or not (deployment or pod):
            return Refused("identity_missing")
        if not LABEL.fullmatch(namespace) or any(
            value is not None and not dns_name(value) for value in (deployment, pod)
        ):
            return Refused("identity_invalid")
        if "cluster" in labels and labels["cluster"] != self.cluster:
            return Refused("out_of_scope")
        if not any(ns == namespace for ns, _ in self.snapshot.targets):
            return Refused("out_of_scope")
        if pod:
            key = ("pod", namespace, pod)
            if key not in self.cache:
                self.cache[key] = self._guard(lambda: self._pod(namespace, pod))
            result = self.cache[key]
            if isinstance(result, Resolved) and deployment and result.name != deployment:
                return Refused("identity_ambiguous")
            return result
        if (namespace, deployment) not in self.snapshot.targets:
            return Refused("out_of_scope")
        key = ("deployment", namespace, deployment)
        if key not in self.cache:
            self.cache[key] = self._guard(lambda: self._deployment(namespace, deployment))
        return self.cache[key]

    def _guard(self, read) -> Resolved | Refused:
        try:
            return read()
        except _Missing:
            return Refused("identity_missing")
        except _Retry as error:
            return Refused(error.reason, retry=True)

    def _match(self, namespace: str, name: str, uid: str) -> Resolved | Refused:
        target = self.snapshot.targets.get((namespace, name))
        if not target:
            return Refused("out_of_scope")
        if target[1] != uid:
            return Refused("identity_changed")
        return Resolved(target[0], uid, namespace, name)

    def _deployment(self, namespace: str, name: str) -> Resolved | Refused:
        value = self._get(
            f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/deployments/{quote(name, safe='')}"
        )
        metadata = value.get("metadata") if isinstance(value, dict) else None
        if (
            not isinstance(metadata, dict)
            or value.get("kind") != "Deployment"
            or metadata.get("name") != name
            or metadata.get("namespace") != namespace
            or not isinstance(metadata.get("uid"), str)
        ):
            raise _Retry()
        return self._match(namespace, name, metadata["uid"])

    def _pod(self, namespace: str, name: str) -> Resolved | Refused:
        ns = quote(namespace, safe="")
        pod = self._get(f"/api/v1/namespaces/{ns}/pods/{quote(name, safe='')}")
        owner = self._controller(pod, "Pod", namespace, name)
        if isinstance(owner, Refused):
            return owner
        if owner.get("kind") != "ReplicaSet" or owner.get("apiVersion") != "apps/v1":
            return Refused("unsupported_identity")
        replica_set = self._get(
            f"/apis/apps/v1/namespaces/{ns}/replicasets/{quote(owner['name'], safe='')}"
        )
        if replica_set.get("metadata", {}).get("uid") != owner.get("uid"):
            return Refused("identity_ambiguous")
        parent = self._controller(replica_set, "ReplicaSet", namespace, owner["name"])
        if isinstance(parent, Refused):
            return parent
        if parent.get("kind") != "Deployment" or parent.get("apiVersion") != "apps/v1":
            return Refused("unsupported_identity")
        return self._match(namespace, parent["name"], parent["uid"])

    @staticmethod
    def _controller(value, kind, namespace, name) -> dict | Refused:
        metadata = value.get("metadata") if isinstance(value, dict) else None
        if (
            not isinstance(metadata, dict)
            or value.get("kind") != kind
            or metadata.get("name") != name
            or metadata.get("namespace") != namespace
        ):
            raise _Retry()
        owners = metadata.get("ownerReferences", [])
        if not isinstance(owners, list) or len(owners) > 16:
            return Refused("identity_ambiguous")
        controllers = [o for o in owners if isinstance(o, dict) and o.get("controller") is True]
        if len(controllers) > 1:
            return Refused("identity_ambiguous")
        if not controllers:
            return Refused("unsupported_identity")
        owner = controllers[0]
        if not dns_name(owner.get("name")) or not isinstance(owner.get("uid"), str):
            return Refused("identity_ambiguous")
        return owner

    def _connect(self) -> httpx.Client:
        if self.client:
            return self.client
        try:
            config = json.loads(Path(os.environ["KUBE_READ_CONFIG"]).read_text(encoding="utf-8"))
            if cluster_fingerprint(config) != self.snapshot.fingerprint:
                raise ValueError
            token = config["token"]
            if not isinstance(token, str) or not token or any(ord(c) <= 32 for c in token):
                raise ValueError
            context = ssl.create_default_context(
                cadata=base64.b64decode(config["ca_data"], validate=True).decode("ascii")
            )
            server = server_identity(config["server"])
        except (KeyError, OSError, ValueError, TypeError, UnicodeError):
            raise _Retry() from None
        self.client = httpx.Client(
            base_url=server,
            verify=context,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
            timeout=httpx.Timeout(4),
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        return self.client

    def _get(self, path: str) -> dict:
        client = self._connect()
        self.requests += 1
        remaining = self.deadline - time.monotonic()
        if self.requests > MAX_REQUESTS or remaining <= 0:
            raise _Retry("budget_exhausted")
        try:
            with client.stream("GET", path, timeout=min(4.0, remaining)) as response:
                if response.status_code == 404:
                    raise _Missing()
                if response.status_code != 200:
                    raise _Retry()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise _Retry()
                body = bytearray()
                # Encoding is pinned to identity above, so decoded bytes are the raw bytes.
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > RESPONSE_BYTES:
                        raise _Retry()
            value = json.loads(body)
        except httpx.HTTPError:
            raise _Retry() from None
        except ValueError:
            raise _Retry() from None
        if not isinstance(value, dict):
            raise _Retry()
        return value
