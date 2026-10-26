"""Scoped Kubernetes observations; credentials and destinations come only from host configuration."""

import base64
import json
import os
import ssl
from decimal import Decimal, DecimalException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler

from cloudops.shared.errors import DomainError


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
