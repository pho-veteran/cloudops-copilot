"""Scoped Kubernetes observations; credentials and destinations come only from host configuration."""

import base64
import json
import os
import ssl
from decimal import Decimal, DecimalException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler

from cloudops.features.workloads.schemas import (
    ConfigReference,
    ObservedContainer,
    ObservedPod,
    ObservationCoverage,
    WorkloadObservationPayload,
)
from cloudops.shared.errors import DomainError

SAFE_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "ContainerCreating",
        "PodInitializing",
        "CreateContainerConfigError",
        "RunContainerError",
        "InvalidImageName",
        "Completed",
    }
)
FAILURE_WAITING_REASONS = {
    "CrashLoopBackOff": "container_crash_loop",
    "ImagePullBackOff": "container_image_pull",
    "ErrImagePull": "container_image_pull",
    "InvalidImageName": "container_image_pull",
    "CreateContainerConfigError": "container_config_error",
    "RunContainerError": "container_config_error",
}


class Kubernetes:
    def __init__(self, path: str | None = None):
        path = path or os.environ.get("KUBE_READ_CONFIG")
        if not path:
            raise DomainError(
                503, "cluster_unavailable", "Configure the scoped cluster connection."
            )
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
            self.server = config["server"].rstrip("/")
            if not self.server.startswith("https://"):
                raise ValueError
            self.token = config["token"]
            context = ssl.create_default_context(
                cadata=base64.b64decode(config["ca_data"]).decode()
            )
            self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
        except (ValueError, KeyError, OSError):
            raise DomainError(
                503, "cluster_unavailable", "The scoped cluster configuration is unavailable."
            ) from None

    def request(self, path: str, *, patch: list | None = None, optional=False):
        request = Request(
            self.server + path,
            data=json.dumps(patch).encode() if patch is not None else None,
            method="PATCH" if patch is not None else "GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json-patch+json"} if patch is not None else {}),
            },
        )
        try:
            with self.opener.open(request, timeout=8) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise ValueError
                return json.loads(payload)
        except HTTPError as error:
            if optional and error.code == 404:
                return {"items": []}
            code = "target_changed" if error.code in (404, 409, 422) else "cluster_unavailable"
            raise DomainError(
                409 if code == "target_changed" else 503,
                code,
                "The cluster target changed or its scoped connection is unavailable. Refresh before trying again.",
            ) from None
        except (URLError, OSError, ValueError):
            raise DomainError(
                503,
                "cluster_unavailable",
                "The scoped cluster request failed; no successful observation is claimed.",
            ) from None

    @staticmethod
    def path(namespace, name):
        return f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/deployments/{quote(name, safe='')}"

    def deployment(self, workload):
        value = self.request(self.path(workload.namespace, workload.name))
        if value["metadata"]["uid"] != workload.uid:
            raise DomainError(
                409,
                "target_changed",
                "Deployment UID changed. Synchronize scope; historical identities remain separate.",
            )
        return value

    def observe(self, workload, previous_payload=None):
        """Read one bounded, UID-owned observation without exposing provider text."""
        deployment = self.deployment(workload)
        metadata = deployment.get("metadata") or {}
        spec = deployment.get("spec") or {}
        status = deployment.get("status")
        desired = spec.get("replicas", 1)
        generation = metadata.get("generation", 0)
        template = spec.get("template") or {}
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
        namespace = quote(workload.namespace, safe="")

        missing: list[str] = []
        # A scaled-to-zero Deployment has no rollout generation to observe; any
        # present status is sufficient coverage. Live rollouts need the field.
        if not isinstance(status, dict) or (desired > 0 and "observedGeneration" not in status):
            add_missing(missing, "deployment_status")

        replicasets, replicasets_truncated = self.collection(
            f"/apis/apps/v1/namespaces/{namespace}/replicasets", selector, 100
        )
        if replicasets_truncated:
            add_missing(missing, "pagination")
        current = None
        if not replicasets_truncated:
            candidates = [
                item
                for item in replicasets
                if owned_by(item, metadata.get("uid")) and template_matches(item, template)
            ]
            if len(candidates) == 1:
                current = candidates[0]
        if current is None:
            add_missing(missing, "current_replicaset")

        pods = []
        if current is not None:
            pod_selector = ((current.get("spec") or {}).get("selector") or {}).get(
                "matchLabels"
            ) or selector
            items, pods_truncated = self.collection(
                f"/api/v1/namespaces/{namespace}/pods", pod_selector, 200
            )
            if pods_truncated:
                add_missing(missing, "pagination")
            current_uid = (current.get("metadata") or {}).get("uid")
            pods = [
                item
                for item in items
                if owned_by(item, current_uid) and not terminating(item) and not terminal(item)
            ]
            if len(pods) < desired:
                add_missing(missing, "pods")

        observed_pods: list[ObservedPod] = []
        reasons: set[str] = set()
        restart_total = 0
        ready_containers = 0
        total_containers = 0
        for pod in pods:
            pod_status = pod.get("status")
            if not isinstance(pod_status, dict):
                add_missing(missing, "pod_status")
                continue
            pod_spec = pod.get("spec") or {}
            regular = pod_status.get("containerStatuses")
            init = pod_status.get("initContainerStatuses") or []
            if not isinstance(regular, list) or not isinstance(init, list):
                add_missing(missing, "container_status")
                continue
            if {item.get("name") for item in regular} != {
                container.get("name") for container in pod_spec.get("containers", [])
            } or {item.get("name") for item in init} != {
                container.get("name") for container in pod_spec.get("initContainers", [])
            }:
                add_missing(missing, "container_status")

            containers = []
            for item in regular:
                name = item.get("name")
                if not isinstance(name, str):
                    add_missing(missing, "container_status")
                    continue
                restarts = restart_count(item)
                restart_total += restarts
                total_containers += 1
                ready = bool(item.get("ready"))
                ready_containers += int(ready)
                waiting = waiting_reason(item)
                if waiting in FAILURE_WAITING_REASONS:
                    reasons.add(FAILURE_WAITING_REASONS[waiting])
                if nonzero_termination(item):
                    reasons.add("container_terminated_nonzero")
                containers.append(
                    ObservedContainer(
                        name=name,
                        init=False,
                        ready=ready,
                        restart_count=restarts,
                        waiting_reason=waiting,
                    )
                )
            for item in init:
                name = item.get("name")
                if not isinstance(name, str):
                    add_missing(missing, "container_status")
                    continue
                restarts = restart_count(item)
                restart_total += restarts
                waiting = waiting_reason(item)
                if waiting in FAILURE_WAITING_REASONS:
                    reasons.add(FAILURE_WAITING_REASONS[waiting])
                if nonzero_termination(item):
                    reasons.add("container_terminated_nonzero")
                containers.append(
                    ObservedContainer(
                        name=name,
                        init=True,
                        ready=bool(item.get("ready")),
                        restart_count=restarts,
                        waiting_reason=waiting,
                    )
                )

            conditions = {
                condition.get("type"): condition
                for condition in pod_status.get("conditions", [])
                if isinstance(condition, dict)
            }
            scheduled = conditions.get("PodScheduled") or {}
            if scheduled.get("status") == "False" and scheduled.get("reason") == "Unschedulable":
                reasons.add("pod_unschedulable")
            observed_pods.append(
                ObservedPod(
                    uid=(pod.get("metadata") or {}).get("uid", ""),
                    name=(pod.get("metadata") or {}).get("name", ""),
                    phase=pod_status.get("phase"),
                    ready=(conditions.get("Ready") or {}).get("status") == "True",
                    containers=containers,
                )
            )

        if isinstance(status, dict):
            for condition in status.get("conditions", []) or []:
                if (
                    condition.get("type") == "Progressing"
                    and condition.get("status") == "False"
                    and condition.get("reason") == "ProgressDeadlineExceeded"
                ):
                    reasons.add("progress_deadline_exceeded")

        deployment_ready = (
            isinstance(status, dict)
            and desired > 0
            and status.get("observedGeneration", -1) >= generation
            and all(
                status.get(field, 0) == desired
                for field in ("readyReplicas", "availableReplicas", "updatedReplicas", "replicas")
            )
        )
        complete = not missing
        if desired == 0:
            state = "scaled_to_zero"
        elif reasons:
            state = "degraded"
        elif "deployment_status" in missing:
            state = "unknown"
        elif (
            complete
            and deployment_ready
            and ready_containers == total_containers
            and all(pod.ready for pod in observed_pods)
        ):
            state = "healthy"
        else:
            state = "progressing"
        if state == "progressing":
            reasons.add("rollout_incomplete")

        return WorkloadObservationPayload(
            deployment_uid=metadata.get("uid", ""),
            generation=generation,
            resource_version=metadata.get("resourceVersion", ""),
            observed_generation=status.get("observedGeneration")
            if isinstance(status, dict)
            else None,
            desired_replicas=desired,
            ready_replicas=status_count(status, "readyReplicas"),
            available_replicas=status_count(status, "availableReplicas"),
            updated_replicas=status_count(status, "updatedReplicas"),
            total_replicas=status_count(status, "replicas"),
            status=state,
            reasons=sorted(reasons),
            coverage=ObservationCoverage(complete=complete, missing_sources=missing),
            pods=observed_pods,
            restart_total=restart_total,
            restart_delta=restart_delta(previous_payload, observed_pods),
            ready_containers=ready_containers,
            total_containers=total_containers,
            template_fields=template_fields(template),
            config_references=config_references(template),
        )

    def collection(self, prefix, selector, limit):
        query = {"limit": limit}
        labels = selector_text(selector)
        if labels:
            query["labelSelector"] = labels
        value = self.request(f"{prefix}?{urlencode(query)}")
        items = value.get("items")
        if not isinstance(items, list):
            return [], True
        return items, bool((value.get("metadata") or {}).get("continue"))

    def controllers(self, workload):
        ns = quote(workload.namespace, safe="")
        for api, collection, optional in (
            ("autoscaling/v2", "horizontalpodautoscalers", False),
            ("autoscaling.k8s.io/v1", "verticalpodautoscalers", True),
        ):
            values = self.request(f"/apis/{api}/namespaces/{ns}/{collection}", optional=optional)[
                "items"
            ]
            if any(
                item.get("spec", {})
                .get("scaleTargetRef", item.get("spec", {}).get("targetRef", {}))
                .get("name")
                == workload.name
                for item in values
            ):
                raise DomainError(
                    409,
                    "controller_conflict",
                    "A workload controller owns this target. Manual review is required.",
                )

    def capacity(self, deployment, replicas):
        delta = replicas - deployment["spec"].get("replicas", 1)
        if delta <= 0:
            return
        nodes = self.request("/api/v1/nodes")["items"]
        pods = self.request("/api/v1/pods")["items"]
        template = pod_requests(deployment["spec"]["template"]["spec"])
        target_spec = deployment["spec"]["template"]["spec"]
        if any(
            set(c.get("resources", {}).get("requests", {})) - {"cpu", "memory"}
            for c in [
                target_spec,
                *target_spec.get("containers", []),
                *target_spec.get("initContainers", []),
            ]
        ):
            raise DomainError(
                409, "capacity_unproven", "Extended resource capacity requires manual review."
            )
        # The supported local topology is one schedulable node. Do not infer capacity
        # by aggregating incompatible nodes, affinity or storage placements.
        available = [
            n
            for n in nodes
            if not n.get("spec", {}).get("unschedulable") and not n.get("spec", {}).get("taints")
        ]
        spec = deployment["spec"]["template"]["spec"]
        if (
            len(available) != 1
            or any(
                spec.get(field)
                for field in (
                    "affinity",
                    "nodeSelector",
                    "topologySpreadConstraints",
                    "volumes",
                    "tolerations",
                    "nodeName",
                    "runtimeClassName",
                    "schedulingGates",
                    "hostNetwork",
                )
            )
            or spec.get("schedulerName", "default-scheduler") != "default-scheduler"
            or any(
                p.get("hostPort") for c in spec.get("containers", []) for p in c.get("ports", [])
            )
        ):
            raise DomainError(
                409,
                "capacity_unproven",
                "This scale requires unsupported placement or capacity proof; use manual guidance.",
            )
        node = available[0]
        conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
        if conditions.get("Ready") != "True" or any(
            conditions.get(k) != "False" for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
        ):
            raise DomainError(409, "capacity_unproven", "The node is not ready without pressure.")
        allocated = {"cpu": Decimal(0), "memory": Decimal(0)}
        allocated_pods = 0
        for pod in pods:
            if pod.get("spec", {}).get("nodeName") == node["metadata"]["name"] and pod.get(
                "status", {}
            ).get("phase") not in ("Succeeded", "Failed"):
                allocated_pods += 1
                for resource, value in pod_requests(pod["spec"]).items():
                    allocated[resource] += value
        if allocated_pods + delta > quantity(node["status"]["allocatable"].get("pods", 0)) or any(
            allocated[k] + delta * template[k] > quantity(node["status"]["allocatable"][k])
            for k in allocated
        ):
            raise DomainError(
                409, "capacity_unproven", "Requested replicas exceed proven allocatable requests."
            )


def quantity(value):
    text = str(value)
    for suffix, multiplier in (
        ("Ki", 1024),
        ("Mi", 1024**2),
        ("Gi", 1024**3),
        ("Ti", 1024**4),
        ("Pi", 1024**5),
        ("Ei", 1024**6),
        ("n", Decimal("0.000000001")),
        ("u", Decimal("0.000001")),
        ("m", Decimal("0.001")),
        ("k", 1000),
        ("M", 1000**2),
        ("G", 1000**3),
        ("T", 1000**4),
        ("P", 1000**5),
        ("E", 1000**6),
    ):
        if text.endswith(suffix):
            return checked_decimal(text[: -len(suffix)]) * multiplier
    return checked_decimal(text)


def checked_decimal(value):
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0:
            raise ValueError
        return parsed
    except (DecimalException, ValueError):
        raise DomainError(
            409, "capacity_unproven", "A resource quantity cannot be safely evaluated."
        ) from None


def pod_requests(spec):
    # Summing all init containers is deliberately conservative, including sidecars.
    result = {"cpu": Decimal(0), "memory": Decimal(0)}
    for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
        for resource in result:
            result[resource] += quantity(
                container.get("resources", {}).get("requests", {}).get(resource, 0)
            )
    for resource in result:
        result[resource] = max(
            result[resource],
            quantity(spec.get("resources", {}).get("requests", {}).get(resource, 0)),
        )
        result[resource] += quantity(spec.get("overhead", {}).get(resource, 0))
    return result


def healthy(deployment):
    spec, status = deployment["spec"], deployment.get("status", {})
    desired = spec.get("replicas", 1)
    return (
        desired > 0
        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        and all(
            status.get(field, 0) == desired
            for field in ("readyReplicas", "availableReplicas", "updatedReplicas", "replicas")
        )
    )


def snapshot(deployment):
    return {
        "uid": deployment["metadata"]["uid"],
        "replicas": deployment["spec"].get("replicas", 1),
        "containers": [
            {"name": c["name"], "image": c["image"], "resources": c.get("resources", {})}
            for c in deployment["spec"]["template"]["spec"]["containers"]
        ],
        "limitations": "Selected image/resource fields only; referenced configuration, secrets and external effects are not captured.",
    }


def add_missing(missing, code):
    if code not in missing:
        missing.append(code)


def selector_text(labels):
    return ",".join(f"{key}={value}" for key, value in sorted((labels or {}).items()))


def owned_by(item, uid):
    return bool(uid) and any(
        reference.get("uid") == uid and reference.get("controller") is True
        for reference in (item.get("metadata") or {}).get("ownerReferences", [])
    )


def template_matches(replicaset, template):
    expected = normalized_template(template)
    return normalized_template((replicaset.get("spec") or {}).get("template") or {}) == expected


def normalized_template(template):
    labels = dict((template.get("metadata") or {}).get("labels") or {})
    labels.pop("pod-template-hash", None)
    return {"labels": labels, "spec": template.get("spec") or {}}


def terminating(pod):
    return bool((pod.get("metadata") or {}).get("deletionTimestamp"))


def terminal(pod):
    return (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")


def restart_count(container):
    value = container.get("restartCount", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def waiting_reason(container):
    waiting = ((container.get("state") or {}).get("waiting") or {}).get("reason")
    if waiting is None:
        return None
    return waiting if waiting in SAFE_WAITING_REASONS else "Unknown"


def nonzero_termination(container):
    for state in (container.get("state"), container.get("lastState")):
        terminated = (state or {}).get("terminated") or {}
        code = terminated.get("exitCode")
        if isinstance(code, int) and code != 0:
            return True
    return False


def status_count(status, field):
    value = status.get(field, 0) if isinstance(status, dict) else 0
    return value if isinstance(value, int) and value >= 0 else 0


def restart_delta(previous_payload, pods):
    previous = (
        previous_payload.model_dump()
        if hasattr(previous_payload, "model_dump")
        else previous_payload
    )
    if not isinstance(previous, dict):
        return None
    prior = {}
    for pod in previous.get("pods", []):
        for container in pod.get("containers", []):
            prior[(pod.get("uid"), container.get("name"))] = container.get("restart_count", 0)
    current = {}
    for pod in pods:
        for container in pod.containers:
            current[(pod.uid, container.name)] = container.restart_count
    return sum(max(0, value - prior.get(key, value)) for key, value in current.items())


def template_fields(template):
    spec = template.get("spec") or {}
    return {
        "service_account_name": spec.get("serviceAccountName"),
        "containers": [container_fields(item) for item in spec.get("containers", [])],
        "init_containers": [container_fields(item) for item in spec.get("initContainers", [])],
    }


def container_fields(container):
    resources = container.get("resources") or {}
    return {
        "name": container.get("name"),
        "image": container.get("image"),
        "image_pull_policy": container.get("imagePullPolicy"),
        "env": [entry.get("name") for entry in container.get("env", []) if entry.get("name")],
        "resources": {
            kind: {
                name: value
                for name, value in (resources.get(kind) or {}).items()
                if name in {"cpu", "memory"}
            }
            for kind in ("requests", "limits")
        },
        "ports": [
            {"name": port.get("name"), "container_port": port.get("containerPort")}
            for port in container.get("ports", [])
        ],
    }


def config_references(template):
    spec = template.get("spec") or {}
    references = []
    seen = set()

    def add(kind, name, source):
        key = (kind, name, source)
        if isinstance(name, str) and name and key not in seen:
            seen.add(key)
            references.append(ConfigReference(kind=kind, name=name, source=source))

    for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
        for item in container.get("envFrom", []) or []:
            config_map = item.get("configMapRef") or {}
            secret = item.get("secretRef") or {}
            add("config_map", config_map.get("name"), "env_from")
            add("secret", secret.get("name"), "env_from")
        for entry in container.get("env", []) or []:
            value_from = entry.get("valueFrom") or {}
            secret = value_from.get("secretKeyRef") or {}
            config_map = value_from.get("configMapKeyRef") or {}
            add("secret", secret.get("name"), "env")
            add("config_map", config_map.get("name"), "env")
    for volume in spec.get("volumes", []) or []:
        secret = volume.get("secret") or {}
        config_map = volume.get("configMap") or {}
        add("secret", secret.get("secretName"), "volume")
        add("config_map", config_map.get("name"), "volume")
    add("service_account", spec.get("serviceAccountName"), "pod_spec")
    return references
