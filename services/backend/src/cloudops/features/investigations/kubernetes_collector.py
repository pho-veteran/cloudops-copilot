"""Read-only, namespace/UID-scoped Kubernetes incident evidence collection.

The collector never reads Secret objects. Every read is bracketed against the
configured Deployment UID so that a replaced target fails closed instead of
publishing evidence for a different workload.
"""

import asyncio
import base64
import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
import httpx

from cloudops.features.investigations.schemas import Coverage, SourceResult, SourceTimes

MAX_RESPONSE_BYTES = 2_000_000
MAX_LIST_PAGES = 5
PAGE_SIZE = 200
EVENT_LIMIT = 100


class ScopedK8sError(Exception):
    """Internal read failure; adapters translate it before publication."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _segment(value: str) -> str:
    """Quote one scope identifier so it cannot become more than one path segment.

    ``quote`` leaves ``.`` unescaped, so empty or dot-only values would be
    collapsed by path normalization and could escape the intended collection.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ScopedK8sError("target_changed")
    return quote(value, safe="")


def _decode_ca(value: str) -> str:
    return base64.b64decode(value, validate=True).decode("ascii")


class ScopedKubernetesClient:
    """GET-only client bound to one host-configured cluster and bearer token."""

    def __init__(self, config_path: str | None = None, timeout: float = 5.0, *, deadline=None):
        path = config_path if config_path is not None else os.environ.get("KUBE_READ_CONFIG")
        if not path:
            raise ScopedK8sError("cluster_unavailable")
        self._config_path = path
        self._timeout = timeout
        self._deadline = deadline
        self._ssl = None
        self._transport = None
        self._server = ""
        self.truncated = False
        if config_path is None:
            self._load()

    def _load(self) -> None:
        try:
            config = json.loads(Path(self._config_path).read_text(encoding="utf-8"))
            server = config["server"].rstrip("/")
            parsed = urlsplit(server)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "\\" in server
            ):
                raise ValueError
            token = config["token"]
            if (
                not isinstance(token, str)
                or not token
                or any(ord(c) < 32 or ord(c) == 127 for c in token)
            ):
                raise ValueError
            context = ssl.create_default_context(cadata=_decode_ca(config["ca_data"]))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            self._server = server
            self._token = token
            self._ssl = context
        except (KeyError, OSError, ValueError, TypeError):
            raise ScopedK8sError("cluster_unavailable") from None

    def _request(self, path: str, params: dict | None = None, *, accept=None) -> bytes:
        if self._ssl is None:
            self._load()
        return asyncio.run(self._read(path, params, accept))

    async def _read(self, path, params, accept) -> bytes:
        remaining = (
            min(self._timeout, self._deadline - time.monotonic())
            if self._deadline is not None
            else self._timeout
        )
        if remaining <= 0:
            raise ScopedK8sError("timeout")
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(
                    verify=self._ssl,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(min(4.0, remaining)),
                    transport=self._transport,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Accept": accept or "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as client:
                    async with client.stream("GET", self._server + path, params=params) as response:
                        if response.status_code in (401, 403):
                            raise ScopedK8sError("forbidden")
                        if response.status_code == 400:
                            raise ScopedK8sError("container_not_ready")
                        if response.status_code in (404, 409, 422):
                            raise ScopedK8sError("target_changed")
                        if response.status_code != 200:
                            raise ScopedK8sError("cluster_unavailable")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise ScopedK8sError("truncated")
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise ScopedK8sError("truncated")
                        return bytes(body)
        except (TimeoutError, httpx.TimeoutException):
            raise ScopedK8sError("timeout") from None
        except (httpx.HTTPError, OSError, ValueError):
            raise ScopedK8sError("cluster_unavailable") from None

    def _request_json(self, path: str, params: dict | None = None, *, accept=None) -> dict:
        body = self._request(path, params, accept=accept)
        try:
            value = json.loads(body)
        except ValueError:
            raise ScopedK8sError("cluster_unavailable") from None
        if not isinstance(value, dict):
            raise ScopedK8sError("cluster_unavailable")
        return value

    def _request_text(self, path: str, params: dict | None = None) -> str:
        return self._request(path, params).decode("utf-8", "replace")

    def _list_items(self, path: str, kind: str, namespace: str) -> list[dict]:
        items: list[dict] = []
        token: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = self._request_json(
                path,
                {"limit": PAGE_SIZE, **({"continue": token} if token else {})},
            )
            if page.get("kind") not in (None, kind):
                raise ScopedK8sError("cluster_unavailable")
            values = page.get("items")
            if not isinstance(values, list):
                raise ScopedK8sError("cluster_unavailable")
            if len(values) > PAGE_SIZE:
                self.truncated = True
            for value in values[:PAGE_SIZE]:
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                # A namespaced collection must never smuggle a foreign object.
                if metadata.get("namespace") != namespace:
                    continue
                items.append(value)
            metadata = page.get("metadata", {})
            token = metadata.get("continue") if isinstance(metadata, dict) else None
            if not token:
                break
        if token:
            self.truncated = True
        return items

    def get_deployment(self, namespace: str, name: str, expected_uid: str) -> dict:
        value = self._request_json(
            f"/apis/apps/v1/namespaces/{_segment(namespace)}/deployments/{_segment(name)}"
        )
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("uid") != expected_uid:
            raise ScopedK8sError("target_changed")
        if metadata.get("namespace") != namespace:
            raise ScopedK8sError("target_changed")
        if metadata.get("name") != name:
            raise ScopedK8sError("target_changed")
        return value

    def get_configmap_metadata(self, namespace: str, name: str) -> dict:
        value = self._request_json(
            f"/api/v1/namespaces/{_segment(namespace)}/configmaps/{_segment(name)}",
            accept="application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1",
        )
        metadata = _metadata(value)
        if (
            value.get("kind") != "PartialObjectMetadata"
            or value.get("apiVersion") != "meta.k8s.io/v1"
            or "data" in value
            or "binaryData" in value
        ):
            raise ScopedK8sError("cluster_unavailable")
        if (
            metadata.get("namespace") != namespace
            or metadata.get("name") != name
            or not _uid(value)
        ):
            raise ScopedK8sError("target_changed")
        return value

    def get_pod(
        self, namespace: str, name: str, expected_uid: str, replicaset_uids: set[str]
    ) -> dict:
        value = self._request_json(
            f"/api/v1/namespaces/{_segment(namespace)}/pods/{_segment(name)}"
        )
        if (
            _uid(value) != expected_uid
            or _metadata(value).get("namespace") != namespace
            or _metadata(value).get("name") != name
            or _controller_uid(value, "ReplicaSet") not in replicaset_uids
        ):
            raise ScopedK8sError("target_changed")
        return value

    def list_owned_replicasets(self, namespace: str, deployment_uid: str) -> list[dict]:
        values = self._list_items(
            f"/apis/apps/v1/namespaces/{_segment(namespace)}/replicasets",
            "ReplicaSetList",
            namespace,
        )
        owned = [
            value for value in values if _controller_uid(value, "Deployment") == deployment_uid
        ]
        return sorted(owned, key=lambda value: _uid(value) or "")

    def list_owned_pods(self, namespace: str, replicaset_uids: set[str]) -> list[dict]:
        values = self._list_items(
            f"/api/v1/namespaces/{_segment(namespace)}/pods", "PodList", namespace
        )
        return _owned_pods(values, replicaset_uids, namespace)

    def list_namespace_events(self, namespace: str, involved_uids: set[str]) -> list[dict]:
        values = self._list_items(
            f"/api/v1/namespaces/{_segment(namespace)}/events", "EventList", namespace
        )
        selected = [
            value
            for value in values
            if isinstance(value.get("involvedObject"), dict)
            and value["involvedObject"].get("uid") in involved_uids
        ]
        selected.sort(
            key=lambda value: str(
                value.get("lastTimestamp")
                or value.get("eventTime")
                or value.get("metadata", {}).get("creationTimestamp", "")
            ),
            reverse=True,
        )
        return selected

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: str,
        previous: bool = False,
        since_time: str | None = None,
        line_limit: int = 200,
    ) -> tuple[str, bool]:
        text = self._request_text(
            f"/api/v1/namespaces/{_segment(namespace)}/pods/{_segment(pod_name)}/log",
            {
                "container": container_name,
                "follow": "false",
                "timestamps": "true",
                "tailLines": str(min(max(line_limit, 0), 200)),
                "previous": "true" if previous else "false",
                "sinceTime": since_time,
                "limitBytes": 32768,
            },
        )
        return text, len(text.splitlines()) >= min(max(line_limit, 0), 200) or len(
            text.encode("utf-8")
        ) >= 32768


def _scoped_client(deadline) -> ScopedKubernetesClient:
    """The single construction path adapters use, so tests can replace it."""
    return ScopedKubernetesClient(deadline=deadline)


# --- Adapters ---------------------------------------------------------------

_NOT_APPLICABLE = object()
_REASONS = {
    "target_changed": "identity_changed",
    "forbidden": "forbidden",
    "cluster_unavailable": "source_unavailable",
    "timeout": "timeout",
    "truncated": "truncated",
    "ownership_unproven": "ownership_unproven",
    "no_samples": "no_samples",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unavailable(key: str, payload_kind: str, reason: str) -> SourceResult:
    now = _now()
    return SourceResult(
        source_key=key,
        payload_kind=payload_kind,
        payload_version=1,
        coverage=Coverage(status="unavailable", reason_codes=[reason]),
        source_times=SourceTimes(observed_at=None, collected_at=now, time_relation="not_captured"),
        query_metadata=None,
        redacted_bounded_payload=None,
    )


def _not_applicable(key: str, payload_kind: str) -> SourceResult:
    now = _now()
    return SourceResult(
        source_key=key,
        payload_kind=payload_kind,
        payload_version=1,
        coverage=Coverage(status="not_applicable", reason_codes=["not_applicable"]),
        source_times=SourceTimes(observed_at=None, collected_at=now, time_relation="not_captured"),
        query_metadata=None,
        redacted_bounded_payload=None,
    )


def _time_relation(context, observed_at, timestamps, payload_kind) -> str:
    window = context.immutable_window
    if window is None:
        return "current_observation"
    if payload_kind == "logs" and (
        not timestamps or any(value < window.start for value in timestamps)
    ):
        return "not_captured"
    if observed_at is not None and observed_at > window.end:
        return "extends_after_window"
    if any(value > window.end for value in timestamps):
        return "extends_after_window"
    return "within_requested_window"


def _guard(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ScopedK8sError("timeout")


def _scoped_result(
    key: str,
    payload_kind: str,
    build,
    context,
    timeout_seconds: float,
) -> SourceResult:
    """Run one adapter body; internal failures become published coverage codes."""
    if context.immutable_window is None:
        return _unavailable(key, payload_kind, "not_captured")
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        outcome = build(deadline)
        _guard(deadline)
    except ScopedK8sError as error:
        return _unavailable(key, payload_kind, _REASONS.get(error.code, "source_unavailable"))
    if outcome is _NOT_APPLICABLE:
        return _not_applicable(key, payload_kind)
    if len(outcome) == 4:
        payload, observed_at, timestamps, reason_codes = outcome
    else:
        payload, observed_at, timestamps = outcome
        reason_codes = ()
    now = _now()
    return SourceResult(
        source_key=key,
        payload_kind=payload_kind,
        payload_version=1,
        coverage=Coverage(
            status="partial" if reason_codes else "available",
            reason_codes=sorted(set(reason_codes)),
        ),
        source_times=SourceTimes(
            observed_at=observed_at,
            collected_at=now,
            time_relation=_time_relation(context, observed_at, timestamps, payload_kind),
        ),
        query_metadata=None,
        redacted_bounded_payload=payload,
    )


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metadata(value) -> dict:
    metadata = value.get("metadata") if isinstance(value, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def _uid(value) -> str | None:
    return _text(_metadata(value).get("uid"))


def _conditions(value) -> list[dict]:
    conditions = value.get("conditions") if isinstance(value, dict) else None
    result = []
    for condition in conditions if isinstance(conditions, list) else []:
        if not isinstance(condition, dict):
            continue
        summary = {
            key: _text(condition.get(key))
            for key in ("type", "status", "reason", "message")
            if _text(condition.get(key))
        }
        if summary:
            result.append(summary)
    return result


def _deployment_summary(value: dict) -> dict:
    metadata = _metadata(value)
    spec = value.get("spec") if isinstance(value.get("spec"), dict) else {}
    status = value.get("status") if isinstance(value.get("status"), dict) else {}
    summary = {
        "uid": _text(metadata.get("uid")),
        "name": _text(metadata.get("name")),
        "namespace": _text(metadata.get("namespace")),
        "generation": _integer(metadata.get("generation")),
        "replicas": _integer(spec.get("replicas")),
        "revision": _text(
            (metadata.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        ),
        "template": _template_summary(value),
        "ready_replicas": _integer(status.get("readyReplicas")),
        "available_replicas": _integer(status.get("availableReplicas")),
        "conditions": _conditions(status),
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}


def _state_summary(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ("waiting", "running", "terminated"):
        detail = value.get(name)
        if not isinstance(detail, dict):
            continue
        summary = {
            "reason": _text(detail.get("reason")),
            "message": _text(detail.get("message")),
            "started_at": _text(detail.get("startedAt")),
            "finished_at": _text(detail.get("finishedAt")),
            "exit_code": _integer(detail.get("exitCode")),
        }
        summary = {key: item for key, item in summary.items() if item is not None}
        if summary:
            result[name] = summary
    return result


def _container_entries(pod: dict):
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    entries = []
    for field, status_field, kind in (
        ("containers", "containerStatuses", "regular"),
        ("initContainers", "initContainerStatuses", "init"),
    ):
        statuses = {
            entry["name"]: entry
            for entry in status.get(status_field) or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        for container in spec.get(field) or []:
            if isinstance(container, dict) and isinstance(container.get("name"), str):
                entries.append(
                    (container["name"], statuses.get(container["name"]), kind, container)
                )
    return sorted(entries, key=lambda entry: entry[0])


def _template_summary(value: dict) -> dict:
    spec = value.get("spec") if isinstance(value.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    containers = []
    for name, _, kind, container in _container_entries(template):
        item = {"name": name, "kind": kind, "image": _text(container.get("image"))}
        resources = container.get("resources")
        if isinstance(resources, dict):
            item["resources"] = {
                group: {
                    key: _text(values.get(key))
                    for key in ("cpu", "memory")
                    if _text(values.get(key)) is not None
                }
                for group in ("requests", "limits")
                if isinstance(values := resources.get(group), dict)
            }
        containers.append(item)
    return {"containers": containers}


def _containers(pod: dict) -> list[dict]:
    result = []
    for name, status, kind, container in _container_entries(pod):
        entry = status or {}
        summary = {
            "name": name,
            "kind": kind,
            "image": _text(container.get("image")),
            "image_id": _text(entry.get("imageID")),
            "restart_count": _integer(entry.get("restartCount")),
            "state": _state_summary(entry.get("state")),
            "last_state": _state_summary(entry.get("lastState")),
        }
        result.append({key: value for key, value in summary.items() if value not in (None, {})})
    return result


def _pod_summary(pod: dict) -> dict:
    metadata = _metadata(pod)
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    summary = {
        "uid": _text(metadata.get("uid")),
        "name": _text(metadata.get("name")),
        "namespace": _text(metadata.get("namespace")),
        # The storage allow-list has no separate phase field; status carries it.
        "status": _text(status.get("phase")),
        "conditions": _conditions(status),
        "containers": _containers(pod),
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}


def _event_summary(event: dict) -> dict:
    metadata = _metadata(event)
    involved = event.get("involvedObject") if isinstance(event.get("involvedObject"), dict) else {}
    summary = {
        "uid": _text(metadata.get("uid")),
        "name": _text(metadata.get("name")),
        "namespace": _text(metadata.get("namespace")),
        "type": _text(event.get("type")),
        "reason": _text(event.get("reason")),
        "message": _text(event.get("message")),
        "count": _integer(event.get("count")),
        "first_seen": _text(event.get("firstTimestamp")) or _text(event.get("eventTime")),
        "last_seen": _text(event.get("lastTimestamp")) or _text(event.get("eventTime")),
        "involved_uid": _text(involved.get("uid")) or _text(event.get("involved_uid")),
    }
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _controller_uid(value: dict, kind: str) -> str | None:
    references = _metadata(value).get("ownerReferences") or []
    controllers = [
        ref for ref in references if isinstance(ref, dict) and ref.get("controller") is True
    ]
    if len(controllers) > 1:
        raise ScopedK8sError("ownership_unproven")
    if (
        not controllers
        or controllers[0].get("kind") != kind
        or controllers[0].get("apiVersion") != "apps/v1"
    ):
        return None
    return _text(controllers[0].get("uid"))


def _owned_replicaset_uids(values: list[dict], deployment_uid: str) -> set[str]:
    return {
        _uid(value)
        for value in values
        if isinstance(value, dict)
        and _controller_uid(value, "Deployment") == deployment_uid
        and _uid(value)
    }


def _scoped_deployment(client, namespace: str, name: str, context) -> dict:
    """Read the scoped Deployment and re-verify its identity.

    The real client already checks the UID, but adapters must fail closed on
    their own because the bracket re-read is the last identity boundary before
    publication and tests may replace the client transport.
    """
    deployment = client.get_deployment(namespace, name, expected_uid=context.workload_uid)
    if (
        _uid(deployment) != context.workload_uid
        or _metadata(deployment).get("namespace") != namespace
        or _metadata(deployment).get("name") != name
    ):
        raise ScopedK8sError("target_changed")
    return deployment


def _owned_pods(values: list[dict], replicaset_uids: set[str], namespace: str) -> list[dict]:
    return sorted(
        (
            value
            for value in values
            if isinstance(value, dict)
            and _metadata(value).get("namespace") == namespace
            and _controller_uid(value, "ReplicaSet") in replicaset_uids
        ),
        key=lambda value: _uid(value) or "",
    )


def _scoped_pod(client, namespace, name, uid, replicaset_uids):
    value = client.get_pod(namespace, name, uid, replicaset_uids)
    if (
        _uid(value) != uid
        or _metadata(value).get("namespace") != namespace
        or _metadata(value).get("name") != name
        or _controller_uid(value, "ReplicaSet") not in replicaset_uids
    ):
        raise ScopedK8sError("target_changed")
    return value


def collect_kubernetes_state(context, timeout_seconds: float) -> SourceResult:
    def build(deadline):
        client = _scoped_client(deadline)
        namespace, name = context.namespace, context.scope_snapshot.name
        _guard(deadline)
        deployment = _scoped_deployment(client, namespace, name, context)
        _guard(deadline)
        replicasets = client.list_owned_replicasets(namespace, context.workload_uid)
        replicaset_uids = _owned_replicaset_uids(replicasets, context.workload_uid)
        _guard(deadline)
        pods = client.list_owned_pods(namespace, replicaset_uids)
        _guard(deadline)
        # Bracket identity: a replaced Deployment fails closed before publication.
        _scoped_deployment(client, namespace, name, context)
        owned = _owned_pods(pods, replicaset_uids, namespace)
        truncated = getattr(client, "truncated", False) or len(owned) > 10 or len(replicasets) > 20
        projected, remaining = [], 20
        for pod in owned[:10]:
            row = _pod_summary(pod)
            entries = row.get("containers", [])
            truncated = truncated or len(entries) > remaining
            row["containers"] = entries[:remaining]
            remaining -= len(row["containers"])
            projected.append(row)
        revisions = []
        for value in sorted(replicasets, key=lambda value: _uid(value) or "")[:20]:
            metadata = _metadata(value)
            revisions.append(
                {
                    "uid": _uid(value),
                    "name": _text(metadata.get("name")),
                    "revision": _text(
                        (metadata.get("annotations") or {}).get("deployment.kubernetes.io/revision")
                    ),
                }
            )
        payload = {
            "deployment": _deployment_summary(deployment),
            "pods": projected,
            "revisions": revisions,
        }
        reasons = ["truncated"] if truncated else []
        if not owned and _integer((deployment.get("spec") or {}).get("replicas")) not in (None, 0):
            reasons.append("no_samples")
        return payload, _now(), (), tuple(reasons)

    return _scoped_result("kubernetes.state", "state", build, context, timeout_seconds)


def collect_kubernetes_events(context, timeout_seconds: float) -> SourceResult:
    def build(deadline):
        client = _scoped_client(deadline)
        namespace, name = context.namespace, context.scope_snapshot.name
        _guard(deadline)
        _scoped_deployment(client, namespace, name, context)
        _guard(deadline)
        replicasets = client.list_owned_replicasets(namespace, context.workload_uid)
        replicaset_uids = _owned_replicaset_uids(replicasets, context.workload_uid)
        _guard(deadline)
        raw_pods = client.list_owned_pods(namespace, replicaset_uids)
        owned = _owned_pods(raw_pods, replicaset_uids, namespace)
        pods = owned[:10]
        involved = {
            context.workload_uid,
            *replicaset_uids,
            *(_uid(pod) for pod in pods if _uid(pod)),
        }
        _guard(deadline)
        events = client.list_namespace_events(namespace, involved)
        _guard(deadline)
        # Bracket identity: a replaced Deployment fails closed before publication.
        _scoped_deployment(client, namespace, name, context)
        selected = []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            involved_uid = _text(
                (event.get("involvedObject") or {}).get("uid")
                if isinstance(event.get("involvedObject"), dict)
                else None
            ) or _text(event.get("involved_uid"))
            if involved_uid in involved:
                selected.append(_event_summary(event))
        selected.sort(key=lambda event: str(event.get("uid", "")))
        truncated = (
            getattr(client, "truncated", False) or len(owned) > 10 or len(selected) > EVENT_LIMIT
        )
        return {"events": selected[:EVENT_LIMIT]}, _now(), (), ("truncated",) if truncated else ()

    return _scoped_result("kubernetes.events", "events", build, context, timeout_seconds)


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    if not text:
        return None
    if "." in text:
        base, rest = text.split(".", 1)
        index = 0
        while index < len(rest) and rest[index].isdigit():
            index += 1
        # Kubernetes uses RFC3339Nano; Python accepts at most microseconds.
        text = base + "." + rest[:index][:6].ljust(6, "0") + rest[index:]
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _log_timestamp(text: str) -> datetime | None:
    latest = None
    for line in text.splitlines()[:200]:
        token = line.split(None, 1)[0] if line.split() else ""
        stamp = _parse_timestamp(token)
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def _collect_logs(context, timeout_seconds: float, *, previous: bool) -> SourceResult:
    key = "kubernetes.logs.previous" if previous else "kubernetes.logs.current"

    def build(deadline):
        client = _scoped_client(deadline)
        namespace, name = context.namespace, context.scope_snapshot.name
        window = context.immutable_window
        since_time = window.start.isoformat() if window is not None else None
        _guard(deadline)
        _scoped_deployment(client, namespace, name, context)
        _guard(deadline)
        replicasets = client.list_owned_replicasets(namespace, context.workload_uid)
        replicaset_uids = _owned_replicaset_uids(replicasets, context.workload_uid)
        _guard(deadline)
        raw_pods = client.list_owned_pods(namespace, replicaset_uids)
        owned = _owned_pods(raw_pods, replicaset_uids, namespace)
        pods = owned[:10]
        logs, containers, timestamps = [], [], []
        read_any = False
        gaps = set()
        entries = [(pod, entry) for pod in pods for entry in _container_entries(pod)]
        if getattr(client, "truncated", False) or len(owned) > 10 or len(entries) > 20:
            gaps.add("truncated")
        if not owned:
            raise ScopedK8sError("no_samples")
        for pod, (container_name, status, kind, _) in entries[:20]:
            metadata = _metadata(pod)
            pod_uid, pod_name = _text(metadata.get("uid")), _text(metadata.get("name"))
            if not pod_uid or not pod_name:
                gaps.add("ownership_unproven")
                continue
            reference = {"name": container_name, "pod_uid": pod_uid, "kind": kind}
            if status is None:
                unscheduled = (pod.get("status") or {}).get("phase") == "Pending" and not (
                    pod.get("spec") or {}
                ).get("nodeName")
                if not unscheduled:
                    gaps.add("no_samples")
                    containers.append({**reference, "previous": "unavailable"})
                    continue
                status = {}
            restart_count = _integer(status.get("restartCount")) or 0
            last_state = status.get("lastState")
            state = status.get("state") if isinstance(status.get("state"), dict) else {}
            started = (
                bool(state.get("running") or state.get("terminated"))
                or restart_count > 0
                or bool(last_state)
                or status.get("started") is True
            )
            if (previous and restart_count == 0 and not last_state) or not started:
                containers.append({**reference, "previous": "not_applicable"})
                continue
            _guard(deadline)
            _scoped_pod(client, namespace, pod_name, pod_uid, replicaset_uids)
            _guard(deadline)
            try:
                text, hit_cap = client.get_pod_logs(
                    namespace,
                    pod_name,
                    container_name,
                    previous=previous,
                    since_time=since_time,
                    line_limit=200,
                )
            except ScopedK8sError as error:
                if error.code != "container_not_ready":
                    raise
                gaps.add("no_samples")
                containers.append({**reference, "previous": "unavailable"})
                _scoped_pod(client, namespace, pod_name, pod_uid, replicaset_uids)
                continue
            _guard(deadline)
            _scoped_pod(client, namespace, pod_name, pod_uid, replicaset_uids)
            if hit_cap:
                gaps.add("truncated")
            read_any = True
            stamp = _log_timestamp(text) if isinstance(text, str) else None
            if stamp is not None:
                timestamps.append(stamp)
                if stamp < window.start:
                    gaps.add("stale")
            elif text:
                gaps.add("no_samples")
            if text:
                entry = {
                    "pod_uid": pod_uid,
                    "container": container_name,
                    "kind": kind,
                    "previous": previous,
                    "text": text,
                }
                if stamp is not None:
                    entry["timestamp"] = stamp.isoformat()
                logs.append(entry)
            containers.append({**reference, "previous": "available"})
        _guard(deadline)
        _scoped_deployment(client, namespace, name, context)
        if not read_any and not gaps:
            return _NOT_APPLICABLE
        if not read_any and "no_samples" in gaps and "truncated" not in gaps:
            raise ScopedK8sError("no_samples")
        observed_at = max(timestamps) if timestamps else None
        payload = {
            "logs": sorted(logs, key=lambda entry: (entry["pod_uid"], entry["container"])),
            "containers": sorted(containers, key=lambda entry: (entry["pod_uid"], entry["name"])),
            "truncated": "truncated" in gaps,
        }
        return payload, observed_at, tuple(timestamps), tuple(gaps)

    return _scoped_result(key, "logs", build, context, timeout_seconds)


def collect_kubernetes_logs_current(context, timeout_seconds: float) -> SourceResult:
    return _collect_logs(context, timeout_seconds, previous=False)


def collect_kubernetes_logs_previous(context, timeout_seconds: float) -> SourceResult:
    return _collect_logs(context, timeout_seconds, previous=True)


def _configmap_references(deployment: dict) -> list[str]:
    names = set()
    spec_value = deployment.get("spec")
    spec = spec_value if isinstance(spec_value, dict) else {}
    template_value = spec.get("template")
    template = template_value if isinstance(template_value, dict) else {}
    pod_spec_value = template.get("spec")
    pod_spec = pod_spec_value if isinstance(pod_spec_value, dict) else {}
    for key in ("containers", "initContainers"):
        for container in pod_spec.get(key) or []:
            if not isinstance(container, dict):
                continue
            for env_from in container.get("envFrom") or []:
                reference = env_from.get("configMapRef") if isinstance(env_from, dict) else None
                if isinstance(reference, dict) and isinstance(reference.get("name"), str):
                    names.add(reference["name"])
            for env in container.get("env") or []:
                value_from = env.get("valueFrom") if isinstance(env, dict) else None
                reference = (
                    value_from.get("configMapKeyRef") if isinstance(value_from, dict) else None
                )
                if isinstance(reference, dict) and isinstance(reference.get("name"), str):
                    names.add(reference["name"])
    for volume in pod_spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        reference = volume.get("configMap")
        if isinstance(reference, dict) and isinstance(reference.get("name"), str):
            names.add(reference["name"])
        projected = volume.get("projected")
        sources = projected.get("sources") if isinstance(projected, dict) else None
        for source in sources or []:
            reference = source.get("configMap") if isinstance(source, dict) else None
            if isinstance(reference, dict) and isinstance(reference.get("name"), str):
                names.add(reference["name"])
    return sorted(names)


def collect_kubernetes_configmap_metadata(context, timeout_seconds: float) -> SourceResult:
    key = "kubernetes.configmap.metadata"
    if os.environ.get("KUBE_CONFIGMAP_ENABLED") != "1":
        return _unavailable(key, "state", "source_not_configured")

    def build(deadline):
        client = _scoped_client(deadline)
        namespace, name = context.namespace, context.scope_snapshot.name
        _guard(deadline)
        deployment = _scoped_deployment(client, namespace, name, context)
        configmaps = []
        references = _configmap_references(deployment)
        for configmap_name in references[:20]:
            _guard(deadline)
            value = client.get_configmap_metadata(namespace, configmap_name)
            if str(value.get("kind", "ConfigMap")).lower() == "secret":
                raise ScopedK8sError("forbidden")
            if value.get("kind") != "PartialObjectMetadata":
                raise ScopedK8sError("cluster_unavailable")
            metadata = _metadata(value)
            summary = {
                "name": _text(metadata.get("name")),
                "namespace": _text(metadata.get("namespace")),
                "uid": _text(metadata.get("uid")),
                "resource_version": _text(metadata.get("resourceVersion")),
            }
            configmaps.append({field: item for field, item in summary.items() if item is not None})
        _guard(deadline)
        # Bracket identity: a replaced Deployment fails closed before publication.
        _scoped_deployment(client, namespace, name, context)
        return (
            {
                "metadata": {
                    "uid": context.workload_uid,
                    "namespace": namespace,
                    "name": name,
                },
                "configmaps": configmaps,
            },
            _now(),
            (),
            ("truncated",) if len(references) > 20 else (),
        )

    return _scoped_result(key, "state", build, context, timeout_seconds)
