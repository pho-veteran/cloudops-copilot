"""Read-only, namespace/UID-scoped Kubernetes incident evidence collection.

The collector never reads Secret objects. Every read is bracketed against the
configured Deployment UID so that a replaced target fails closed instead of
publishing evidence for a different workload.
"""

import base64
import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirected read is not scoped to the configured destination.
        return None


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

    def __init__(self, config_path: str | None = None, timeout: float = 5.0):
        explicit = config_path is not None
        path = config_path if explicit else os.environ.get("KUBE_READ_CONFIG")
        if not path:
            raise ScopedK8sError("cluster_unavailable")
        self._config_path = path
        self._timeout = timeout
        self._opener = None
        self._server = ""
        # An env-selected path is validated eagerly. An explicit path is only
        # loaded on first use so tests can replace the transport.
        if not explicit:
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
            if not isinstance(token, str) or not token:
                raise ValueError
            context = ssl.create_default_context(cadata=_decode_ca(config["ca_data"]))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            self._server = server
            self._token = token
            # Empty ProxyHandler keeps deployment/system proxies and their
            # credentials out of the cluster request path.
            self._opener = build_opener(
                ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect()
            )
        except (KeyError, OSError, ValueError, TypeError):
            raise ScopedK8sError("cluster_unavailable") from None

    def _request(self, path: str, params: dict | None = None) -> bytes:
        if self._opener is None:
            self._load()
        url = self._server + path
        if params:
            url += "?" + urlencode(
                {key: value for key, value in params.items() if value is not None}
            )
        request = Request(
            url,
            method="GET",
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ScopedK8sError("cluster_unavailable")
            return body
        except ScopedK8sError:
            raise
        except HTTPError as error:
            if error.code in (401, 403):
                raise ScopedK8sError("forbidden") from None
            if error.code == 400:
                # A container that never started/has no previous run is a
                # conditional outcome, not a target identity change.
                raise ScopedK8sError("container_not_ready") from None
            if error.code in (404, 409, 422):
                raise ScopedK8sError("target_changed") from None
            raise ScopedK8sError("cluster_unavailable") from None
        except (URLError, OSError, ValueError):
            raise ScopedK8sError("cluster_unavailable") from None

    def _request_json(self, path: str, params: dict | None = None) -> dict:
        body = self._request(path, params)
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
            for value in values:
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                # A namespaced collection must never smuggle a foreign object.
                if metadata.get("namespace") not in (None, namespace):
                    continue
                items.append(value)
            metadata = page.get("metadata", {})
            token = metadata.get("continue") if isinstance(metadata, dict) else None
            if not token:
                break
        return items

    def get_deployment(self, namespace: str, name: str, expected_uid: str) -> dict:
        value = self._request_json(
            f"/apis/apps/v1/namespaces/{_segment(namespace)}/deployments/{_segment(name)}"
        )
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("uid") != expected_uid:
            raise ScopedK8sError("target_changed")
        if metadata.get("namespace") not in (None, namespace):
            raise ScopedK8sError("target_changed")
        if metadata.get("name") not in (None, name):
            raise ScopedK8sError("target_changed")
        return value

    def get_configmap_metadata(self, namespace: str, name: str) -> dict:
        """Read one ConfigMap object; the adapter keeps only identifiers."""
        value = self._request_json(
            f"/api/v1/namespaces/{_segment(namespace)}/configmaps/{_segment(name)}"
        )
        if str(value.get("kind", "ConfigMap")).lower() == "secret":
            raise ScopedK8sError("forbidden")
        return value

    def list_owned_replicasets(self, namespace: str, deployment_uid: str) -> list[dict]:
        values = self._list_items(
            f"/apis/apps/v1/namespaces/{_segment(namespace)}/replicasets",
            "ReplicaSetList",
            namespace,
        )
        owned = [
            value
            for value in values
            if any(
                reference.get("uid") == deployment_uid and reference.get("controller") is True
                for reference in value["metadata"].get("ownerReferences", [])
                if isinstance(reference, dict)
            )
        ]
        return sorted(owned, key=lambda value: str(value["metadata"].get("name", "")))

    def list_owned_pods(self, namespace: str, replicaset_uids: set[str]) -> list[dict]:
        values = self._list_items(
            f"/api/v1/namespaces/{_segment(namespace)}/pods", "PodList", namespace
        )
        owned = [
            value
            for value in values
            if any(
                reference.get("uid") in replicaset_uids
                for reference in value["metadata"].get("ownerReferences", [])
                if isinstance(reference, dict)
            )
        ]
        return sorted(owned, key=lambda value: str(value["metadata"].get("name", "")))

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
        return selected[:EVENT_LIMIT]

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
            },
        )
        return text, len(text.splitlines()) >= min(max(line_limit, 0), 200)


def _scoped_client() -> ScopedKubernetesClient:
    """The single construction path adapters use, so tests can replace it."""
    return ScopedKubernetesClient()


# --- Adapters ---------------------------------------------------------------

_NOT_APPLICABLE = object()
_REASONS = {
    "target_changed": "identity_changed",
    "forbidden": "forbidden",
    "cluster_unavailable": "source_unavailable",
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


def _time_relation(context, observed_at, timestamps) -> str:
    window = context.immutable_window
    if window is None:
        return "current_observation"
    if observed_at is not None and observed_at > window.end:
        return "extends_after_window"
    if any(value > window.end for value in timestamps):
        return "extends_after_window"
    if _now() > window.end:
        return "extends_after_window"
    return "within_requested_window"


def _guard(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ScopedK8sError("cluster_unavailable")


def _scoped_result(
    key: str,
    payload_kind: str,
    build,
    context,
    timeout_seconds: float,
) -> SourceResult:
    """Run one adapter body; internal failures become published coverage codes."""
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        outcome = build(deadline)
    except ScopedK8sError as error:
        return _unavailable(key, payload_kind, _REASONS.get(error.code, "source_unavailable"))
    if outcome is _NOT_APPLICABLE:
        return _not_applicable(key, payload_kind)
    payload, observed_at, timestamps = outcome
    now = _now()
    return SourceResult(
        source_key=key,
        payload_kind=payload_kind,
        payload_version=1,
        coverage=Coverage(status="available", reason_codes=[]),
        source_times=SourceTimes(
            observed_at=observed_at or now,
            collected_at=now,
            time_relation=_time_relation(context, observed_at, timestamps),
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


def _containers(pod: dict) -> list[dict]:
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    statuses = {}
    for entry in status.get("containerStatuses") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            statuses[entry["name"]] = entry
    result = []
    for container in spec.get("containers") or []:
        if not isinstance(container, dict) or not isinstance(container.get("name"), str):
            continue
        entry = statuses.get(container["name"], {})
        summary = {
            "name": container["name"],
            "image": _text(container.get("image")),
            "image_id": _text(entry.get("imageID")),
            "restart_count": _integer(entry.get("restartCount")),
            "state": _state_summary(entry.get("state")),
            "last_state": _state_summary(entry.get("lastState")),
        }
        result.append({key: value for key, value in summary.items() if value not in (None, {})})
    return result[:20]


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


def _owned_replicaset_uids(values: list[dict], deployment_uid: str) -> set[str]:
    uids = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        references = _metadata(value).get("ownerReferences") or []
        if any(
            isinstance(reference, dict)
            and reference.get("uid") == deployment_uid
            and reference.get("controller") is True
            for reference in references
        ):
            uid = _uid(value)
            if uid:
                uids.add(uid)
    return uids


def _scoped_deployment(client, namespace: str, name: str, context) -> dict:
    """Read the scoped Deployment and re-verify its identity.

    The real client already checks the UID, but adapters must fail closed on
    their own because the bracket re-read is the last identity boundary before
    publication and tests may replace the client transport.
    """
    deployment = client.get_deployment(namespace, name, expected_uid=context.workload_uid)
    if _uid(deployment) != context.workload_uid:
        raise ScopedK8sError("target_changed")
    return deployment


def _owned_pods(values: list[dict], replicaset_uids: set[str], namespace: str) -> list[dict]:
    pods = []
    for value in values:
        if not isinstance(value, dict):
            continue
        metadata = _metadata(value)
        if metadata.get("namespace") not in (None, namespace):
            continue
        references = metadata.get("ownerReferences") or []
        if any(
            isinstance(reference, dict) and reference.get("uid") in replicaset_uids
            for reference in references
        ):
            pods.append(value)
    return pods[:10]


def collect_kubernetes_state(context, timeout_seconds: float) -> SourceResult:
    def build(deadline):
        client = _scoped_client()
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
        payload = {
            "deployment": _deployment_summary(deployment),
            "pods": [_pod_summary(pod) for pod in _owned_pods(pods, replicaset_uids, namespace)],
        }
        return payload, _now(), ()

    return _scoped_result("kubernetes.state", "state", build, context, timeout_seconds)


def collect_kubernetes_events(context, timeout_seconds: float) -> SourceResult:
    def build(deadline):
        client = _scoped_client()
        namespace, name = context.namespace, context.scope_snapshot.name
        _guard(deadline)
        _scoped_deployment(client, namespace, name, context)
        _guard(deadline)
        replicasets = client.list_owned_replicasets(namespace, context.workload_uid)
        replicaset_uids = _owned_replicaset_uids(replicasets, context.workload_uid)
        _guard(deadline)
        raw_pods = client.list_owned_pods(namespace, replicaset_uids)
        pods = _owned_pods(raw_pods, replicaset_uids, namespace)
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
        selected.sort(key=lambda event: str(event.get("last_seen", "")), reverse=True)
        return {"events": selected[:100]}, _now(), ()

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


def _container_entries(pod: dict):
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    statuses = {}
    for entry in status.get("containerStatuses") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            statuses[entry["name"]] = entry
    for container in spec.get("containers") or []:
        if isinstance(container, dict) and isinstance(container.get("name"), str):
            yield container["name"], statuses.get(container["name"], {})


def _collect_logs(context, timeout_seconds: float, *, previous: bool) -> SourceResult:
    key = "kubernetes.logs.previous" if previous else "kubernetes.logs.current"

    def build(deadline):
        client = _scoped_client()
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
        pods = _owned_pods(raw_pods, replicaset_uids, namespace)
        logs, containers, timestamps = [], [], []
        read_any = False
        for pod in pods:
            metadata = _metadata(pod)
            pod_uid = _text(metadata.get("uid"))
            pod_name = _text(metadata.get("name"))
            if not pod_uid or not pod_name:
                continue
            for container_name, status in _container_entries(pod):
                restart_count = _integer(status.get("restartCount")) or 0
                last_state = status.get("lastState")
                if (
                    previous
                    and restart_count == 0
                    and not (isinstance(last_state, dict) and last_state)
                ):
                    containers.append(
                        {"name": container_name, "pod_uid": pod_uid, "previous": "not_applicable"}
                    )
                    continue
                _guard(deadline)
                try:
                    text, _truncated = client.get_pod_logs(
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
                    containers.append(
                        {"name": container_name, "pod_uid": pod_uid, "previous": "not_applicable"}
                    )
                    continue
                read_any = True
                stamp = _log_timestamp(text) if isinstance(text, str) else None
                if stamp is not None:
                    timestamps.append(stamp)
                if text:
                    entry = {
                        "pod_uid": pod_uid,
                        "container": container_name,
                        "previous": previous,
                        "text": text,
                    }
                    if stamp is not None:
                        entry["timestamp"] = stamp.isoformat()
                    logs.append(entry)
                containers.append(
                    {"name": container_name, "pod_uid": pod_uid, "previous": "available"}
                )
        _guard(deadline)
        # Bracket identity: a replaced Deployment fails closed before publication.
        _scoped_deployment(client, namespace, name, context)
        if not read_any:
            return _NOT_APPLICABLE
        observed_at = max(timestamps) if timestamps else _now()
        payload = {
            "logs": sorted(logs, key=lambda entry: (entry["pod_uid"], entry["container"])),
            "containers": sorted(containers, key=lambda entry: (entry["pod_uid"], entry["name"])),
        }
        return payload, observed_at, tuple(timestamps)

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
    return sorted(names)[:20]


def collect_kubernetes_configmap_metadata(context, timeout_seconds: float) -> SourceResult:
    key = "kubernetes.configmap.metadata"
    if os.environ.get("KUBE_CONFIGMAP_ENABLED") != "1":
        return _unavailable(key, "state", "source_not_configured")

    def build(deadline):
        client = _scoped_client()
        namespace, name = context.namespace, context.scope_snapshot.name
        _guard(deadline)
        deployment = _scoped_deployment(client, namespace, name, context)
        configmaps = []
        for configmap_name in _configmap_references(deployment):
            _guard(deadline)
            value = client.get_configmap_metadata(namespace, configmap_name)
            if str(value.get("kind", "ConfigMap")).lower() == "secret":
                raise ScopedK8sError("forbidden")
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
        )

    return _scoped_result(key, "state", build, context, timeout_seconds)
