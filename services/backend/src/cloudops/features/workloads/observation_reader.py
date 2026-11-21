"""GET-only fixed-destination reads with one cancellable wall-clock budget."""

import asyncio
import base64
import hashlib
import json
import os
import ssl
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.observation_evaluator import ObservationError, evaluate, items
from cloudops.features.workloads.observation_settings import POLICY
from cloudops.features.workloads.scope_config import cluster_fingerprint


def reader_bindings():
    """Return private reader binding and credential-free public configuration identity."""
    try:
        with Path(os.environ["KUBE_READ_CONFIG"]).open("rb") as stream:
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            return None, None
        config = json.loads(raw)
        token = config["token"]
        if (
            not isinstance(token, str)
            or not token
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
        ):
            return None, None
        return credential_binding(config), binding(config)
    except (AttributeError, KeyError, OSError, ValueError, TypeError, RecursionError):
        return None, None


def reader_binding():
    return reader_bindings()[1]


def credential_binding(config):
    return hashlib.sha256(f"{binding(config)}:{config['token']}".encode()).hexdigest()


def binding(config):
    ca = base64.b64decode(config["ca_data"], validate=True)
    return hashlib.sha256(config["server"].rstrip("/").encode() + b"\n" + ca).hexdigest()


class ObservationReader:
    def __init__(self, *, transport=None, expected_binding=None, expected_fingerprint=None):
        self.transport = transport
        self.expected_binding = expected_binding
        self.expected_fingerprint = expected_fingerprint

    async def read(self, identity, started_at):
        from datetime import timedelta
        from time import monotonic

        clock = monotonic()
        try:
            path = os.environ.get("KUBE_READ_CONFIG")
            if not path:
                raise ObservationError("cluster_unavailable")
            config = json.loads(Path(path).read_text(encoding="utf-8"))
            if (
                self.expected_fingerprint
                and cluster_fingerprint(config) != self.expected_fingerprint
            ):
                raise ObservationError("cluster_identity_mismatch")
            if self.expected_binding and credential_binding(config) != self.expected_binding:
                raise ObservationError("cluster_identity_mismatch")
            server = config["server"].rstrip("/")
            url = urlsplit(server)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or "\\" in server
            ):
                raise ObservationError("cluster_unavailable")
            context = ssl.create_default_context(
                cadata=base64.b64decode(config["ca_data"], validate=True).decode()
            )
            async with asyncio.timeout(POLICY.deadline_seconds):
                async with httpx.AsyncClient(
                    base_url=server,
                    verify=context,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                    timeout=httpx.Timeout(4),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                    headers={
                        "Authorization": "Bearer " + config["token"],
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as client:
                    requests = 0

                    async def get(path, params=None):
                        nonlocal requests
                        requests += 1
                        if requests > POLICY.max_requests:
                            raise ObservationError("budget_exhausted")
                        async with client.stream("GET", path, params=params) as response:
                            if response.status_code in (401, 403):
                                raise ObservationError("permission_denied")
                            if response.status_code == 404:
                                raise ObservationError("target_changed")
                            if response.status_code == 410:
                                raise ObservationError("pagination_invalid")
                            if response.status_code != 200:
                                raise ObservationError("cluster_unavailable")
                            if response.headers.get("content-encoding", "identity") != "identity":
                                raise ObservationError("payload_limit")
                            body = bytearray()
                            async for chunk in response.aiter_raw():
                                body.extend(chunk)
                                if len(body) > POLICY.response_bytes:
                                    raise ObservationError("payload_limit")
                            result = json.loads(body)
                            if not isinstance(result, dict):
                                raise ObservationError()
                            return result

                    async def collection(path, kind, maximum):
                        values, token, version, seen = [], "", None, set()
                        for _ in range(maximum // POLICY.page_size):
                            page = await get(
                                path,
                                {
                                    "limit": POLICY.page_size,
                                    **({"continue": token} if token else {}),
                                },
                            )
                            if page.get("kind") != kind or not isinstance(
                                page.get("metadata"), dict
                            ):
                                raise ObservationError()
                            api_version = "apps/v1" if kind == "ReplicaSetList" else "v1"
                            if page.get("apiVersion") != api_version:
                                raise ObservationError()
                            meta = page["metadata"]
                            rv = meta.get("resourceVersion")
                            if (
                                not isinstance(rv, str)
                                or not rv
                                or (version is not None and rv != version)
                            ):
                                raise ObservationError("pagination_invalid")
                            version = rv
                            values.extend(items(page.get("items"), POLICY.page_size))
                            token = meta.get("continue", "")
                            if not isinstance(token, str) or len(token) > 8192 or token in seen:
                                raise ObservationError("pagination_invalid")
                            if not token:
                                return {
                                    "kind": kind,
                                    "apiVersion": api_version,
                                    "items": values,
                                    "metadata": {"resourceVersion": version},
                                }
                            seen.add(token)
                        raise ObservationError("budget_exhausted")

                    deployment_path = Kubernetes.path(identity.namespace, identity.name)
                    start = await get(deployment_path)
                    if start.get("metadata", {}).get("uid") != identity.uid:
                        raise ObservationError("target_changed")
                    ns = quote(identity.namespace, safe="")
                    rs = await collection(
                        f"/apis/apps/v1/namespaces/{ns}/replicasets",
                        "ReplicaSetList",
                        POLICY.max_replicasets,
                    )
                    pods = await collection(
                        f"/api/v1/namespaces/{ns}/pods", "PodList", POLICY.max_pods
                    )
                    end = await get(deployment_path)
                    observation = evaluate(
                        identity,
                        start,
                        end,
                        rs,
                        pods,
                        started_at,
                        started_at + timedelta(seconds=monotonic() - clock),
                    )
                    if len(observation.model_dump_json().encode()) > POLICY.observation_bytes:
                        raise ObservationError("payload_limit")
                    return observation
        except TimeoutError:
            raise ObservationError("attempt_timeout") from None
        except httpx.TimeoutException:
            raise ObservationError("attempt_timeout") from None
        except (httpx.HTTPError, OSError, ssl.SSLError):
            raise ObservationError("cluster_unavailable") from None
        except (KeyError, ValueError, TypeError):
            raise ObservationError("invalid_response") from None
