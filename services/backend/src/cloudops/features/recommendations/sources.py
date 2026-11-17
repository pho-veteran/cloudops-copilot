"""Fixed read-only Kubernetes/Prometheus sources, with one finite collection budget."""

import asyncio
import base64
import json
import math
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import Field

from cloudops.features.investigations.prometheus_config import escape_promql, load_config
from cloudops.features.recommendations.krr_protocol import metrics_binding
from cloudops.features.recommendations.schemas import (
    Digest,
    Name,
    ReportIdentity,
    ReportWindow,
    SafeModel,
)
from cloudops.features.recommendations.service import digest
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.observation_reader import binding
from cloudops.features.workloads.scope_config import cluster_fingerprint

METRICS = (
    "container_cpu_usage_seconds_total",
    "container_memory_working_set_bytes",
    "kube_pod_info",
    "kube_pod_owner",
    "kube_pod_created",
    "kube_pod_completion_time",
    "kube_pod_container_state_started",
    "kube_pod_container_status_restarts_total",
    "kube_pod_container_status_last_terminated_reason",
    "kube_pod_container_status_last_terminated_timestamp",
    "kube_replicaset_owner",
)
LABELS = {
    "pod",
    "uid",
    "pod_uid",
    "container",
    "reason",
    "owner_kind",
    "owner_name",
    "owner_is_controller",
    "created_by_kind",
    "created_by_name",
    "replicaset",
}
MAX_BYTES = 8 * 1024 * 1024
MAX_SAMPLES = 200_000


class SourceFailure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class OwnershipPolicy(SafeModel):
    schema_version: int = Field(ge=1, le=1)
    cluster_fingerprint: Digest
    operator_managed_uids: list[Name] = Field(min_length=1, max_length=100)
    resource_managers: list[Name] = Field(max_length=16)


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    samples: list[tuple[float, float]]


@dataclass(frozen=True)
class SourceData:
    series: dict[str, list[Series]]
    before: dict
    after: dict
    controllers: dict[str, list[str]]
    policy: OwnershipPolicy
    query_digest: str
    config_digest: str
    collected_at: datetime
    metrics_source_sha256: str | None = None


def parse_matrix(payload, namespace, start, end, budget):
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "success"
        or payload.get("warnings")
    ):
        raise SourceFailure("metrics_unavailable")
    data = payload.get("data", {})
    rows = data.get("result")
    if data.get("resultType") != "matrix" or not isinstance(rows, list) or len(rows) > 256:
        raise SourceFailure("metrics_payload_limit")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceFailure("metrics_invalid")
        metric, values = row.get("metric", {}), row.get("values")
        if not isinstance(metric, dict) or metric.get("namespace") != namespace:
            raise SourceFailure("metric_scope_mismatch")
        if not isinstance(values, list):
            raise SourceFailure("metrics_invalid")
        labels = {k: v for k, v in metric.items() if k in LABELS}
        if any(not isinstance(v, str) or not v or len(v) > 253 for v in labels.values()):
            raise SourceFailure("metrics_invalid")
        if labels.get("uid") and labels.get("pod_uid") and labels["uid"] != labels["pod_uid"]:
            raise SourceFailure("metric_scope_mismatch")
        samples = {}
        for pair in values:
            budget[0] += 1
            if budget[0] > MAX_SAMPLES:
                raise SourceFailure("metrics_payload_limit")
            try:
                t, value = pair
                if isinstance(t, bool) or isinstance(value, bool):
                    raise ValueError
                t, value = float(t), float(value)
                if not math.isfinite(t) or not math.isfinite(value) or value < 0 or t > end:
                    raise ValueError
                if t in samples and samples[t] != value:
                    raise ValueError
                if t >= start:
                    samples[t] = value
            except (ValueError, TypeError, OverflowError):
                raise SourceFailure("metrics_invalid") from None
        result.append(Series(labels, sorted(samples.items())))
    return result


def controller_reasons(deployment, hpas, vpas, policy, deployments=(), pods=()):
    reasons = {"cpu": [], "memory": []}
    uid = deployment.get("metadata", {}).get("uid")
    name = deployment.get("metadata", {}).get("name")
    manual = uid not in policy.operator_managed_uids
    labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    template = deployment.get("spec", {}).get("template", {}).get("spec", {})
    selector = deployment.get("spec", {}).get("selector", {})
    if selector.get("matchExpressions") or not selector.get("matchLabels"):
        for resource in reasons:
            reasons[resource].append("unsupported_selector")
    else:

        def selected_fields(spec):
            return sorted(
                (c["name"], c["image"], json.dumps(c.get("resources", {}), sort_keys=True))
                for c in spec.get("containers", [])
            )

        for pod in pods:
            pod_labels = pod.get("metadata", {}).get("labels", {})
            if not all(
                pod_labels.get(key) == value for key, value in selector["matchLabels"].items()
            ):
                continue
            spec = pod.get("spec", {})
            if any(
                spec.get(k)
                for k in ("initContainers", "ephemeralContainers", "resources", "overhead")
            ) or selected_fields(spec) != selected_fields(template):
                for resource in reasons:
                    reasons[resource].append("unsupported_pod_resource_shape")
    for item in deployments:
        if item.get("metadata", {}).get("uid") == uid:
            continue
        selector = item.get("spec", {}).get("selector", {})
        if selector.get("matchExpressions") or not selector.get("matchLabels"):
            raise SourceFailure("selector_scope_unproven")
        if all(labels.get(key) == value for key, value in selector["matchLabels"].items()):
            for resource in reasons:
                reasons[resource].append("selector_overlap")
    for item in deployment.get("metadata", {}).get("managedFields", []):
        fields = item.get("fieldsV1", {})
        text = json.dumps(fields)
        if '"f:resources"' in text and item.get("manager") not in policy.resource_managers:
            manual = True
    for item in vpas:
        target = item.get("spec", {}).get("targetRef", {})
        if target.get("name") == name:
            manual = True
    for item in hpas:
        spec = item.get("spec", {})
        target = spec.get("scaleTargetRef", {})
        if target.get("name") != name:
            continue
        if target.get("kind") != "Deployment" or target.get("apiVersion") != "apps/v1":
            manual = True
            continue
        metrics = spec.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            manual = True
            continue
        for metric in metrics:
            kind = metric.get("type")
            definition = metric.get("resource" if kind == "Resource" else "containerResource", {})
            resource = definition.get("name")
            if (
                kind not in {"Resource", "ContainerResource"}
                or resource not in reasons
                or definition.get("target", {}).get("type") not in {"Utilization", "AverageValue"}
            ):
                manual = True
            else:
                reasons[resource].append("hpa_resource_owned")
    if manual:
        for resource in reasons:
            reasons[resource].append("resource_automation_manual")
    return {key: sorted(set(value)) for key, value in reasons.items()}


class SourceReader:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def read(self, identity: ReportIdentity, window: ReportWindow) -> SourceData:
        try:
            kube_path = Path(os.environ["KUBE_READ_CONFIG"])
            kube = json.loads(kube_path.read_text())
            prom_path = Path(os.environ["PROMETHEUS_READ_CONFIG"])
            prom = load_config(prom_path)
            metrics_source_sha256 = metrics_binding(prom.model_dump(by_alias=True))
            policy = OwnershipPolicy.model_validate_json(
                Path(os.environ["RECOMMENDATION_SOURCE_POLICY"]).read_text()
            )
            if (
                not identity.cluster_fingerprint
                or cluster_fingerprint(kube) != identity.cluster_fingerprint
                or policy.cluster_fingerprint != identity.cluster_fingerprint
            ):
                raise SourceFailure("cluster_identity_mismatch")
            original_binding = binding(kube)
            # Reuse the observation reader's strict HTTPS origin checks.
            from urllib.parse import urlsplit

            server = kube["server"].rstrip("/")
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
                raise SourceFailure("cluster_unavailable")
            context = ssl.create_default_context(
                cadata=base64.b64decode(kube["ca_data"], validate=True).decode()
            )
            start, end = window.raw_input_start.timestamp(), window.raw_input_end.timestamp()
            seconds = math.ceil(end - start)
            if not 21600 <= seconds <= 22500 or window.end > datetime.now(timezone.utc):
                raise SourceFailure("history_window_unqualified")
            ns = quote(identity.namespace, safe="")
            path = Kubernetes.path(identity.namespace, identity.name)
            queries = {
                metric: metric
                + '{namespace="'
                + escape_promql(identity.namespace)
                + '"}['
                + str(seconds + 1)
                + "s]"
                for metric in METRICS
            }
            total = [0]
            async with (
                asyncio.timeout(45),
                httpx.AsyncClient(
                    verify=context,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                    timeout=5,
                ) as kc,
                httpx.AsyncClient(
                    verify=prom.ssl_context(),
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                    timeout=8,
                ) as pc,
            ):

                async def get(client, url, headers=None, params=None):
                    async with client.stream(
                        "GET", url, headers=headers, params=params
                    ) as response:
                        if response.status_code in (401, 403):
                            raise SourceFailure("source_permission_denied")
                        if response.status_code != 200:
                            raise SourceFailure("source_unavailable")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise SourceFailure("metrics_payload_limit")
                        raw = bytearray()
                        async for chunk in response.aiter_raw():
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise SourceFailure("metrics_payload_limit")
                        return json.loads(raw)

                kh = {"Authorization": "Bearer " + kube["token"], "Accept-Encoding": "identity"}
                ph = {
                    "Accept-Encoding": "identity",
                    **({"Authorization": "Bearer " + prom.token} if prom.token else {}),
                }

                async def controllers(deployment):
                    pods = await get(kc, server + f"/api/v1/namespaces/{ns}/pods", kh)
                    if (
                        pods.get("kind") != "PodList"
                        or not isinstance(pods.get("items"), list)
                        or len(pods["items"]) > 500
                        or pods.get("metadata", {}).get("continue")
                    ):
                        raise SourceFailure("pod_scope_unproven")
                    deployments = await get(
                        kc, server + f"/apis/apps/v1/namespaces/{ns}/deployments", kh
                    )
                    if (
                        deployments.get("kind") != "DeploymentList"
                        or not isinstance(deployments.get("items"), list)
                        or len(deployments["items"]) > 100
                        or deployments.get("metadata", {}).get("continue")
                    ):
                        raise SourceFailure("selector_scope_unproven")
                    hpas = await get(
                        kc,
                        server + f"/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers",
                        kh,
                    )
                    groups = await get(kc, server + "/apis", kh)
                    if groups.get("kind") != "APIGroupList" or not isinstance(
                        groups.get("groups"), list
                    ):
                        raise SourceFailure("controller_discovery_unknown")
                    vpa_group = [
                        x for x in groups["groups"] if x.get("name") == "autoscaling.k8s.io"
                    ]
                    vpas = {"items": []}
                    if vpa_group:
                        versions = {
                            v.get("groupVersion") for x in vpa_group for v in x.get("versions", [])
                        }
                        if "autoscaling.k8s.io/v1" not in versions:
                            raise SourceFailure("controller_discovery_unknown")
                        vpas = await get(
                            kc,
                            server
                            + f"/apis/autoscaling.k8s.io/v1/namespaces/{ns}/verticalpodautoscalers",
                            kh,
                        )
                        if vpas.get("kind") != "VerticalPodAutoscalerList":
                            raise SourceFailure("controller_discovery_unknown")
                    if hpas.get("kind") != "HorizontalPodAutoscalerList" or any(
                        not isinstance(x.get("items"), list)
                        or len(x["items"]) > 100
                        or x.get("metadata", {}).get("continue")
                        for x in (hpas, vpas)
                    ):
                        raise SourceFailure("controller_discovery_unknown")
                    return controller_reasons(
                        deployment,
                        hpas["items"],
                        vpas["items"],
                        policy,
                        deployments["items"],
                        pods["items"],
                    )

                before = await get(kc, server + path, kh)
                first = await controllers(before)
                series = {}
                for metric, query in queries.items():
                    payload = await get(
                        pc, prom.server + "/api/v1/query", ph, {"query": query, "time": end}
                    )
                    series[metric] = parse_matrix(payload, identity.namespace, start, end, total)
                after = await get(kc, server + path, kh)
                last = await controllers(after)
                if first != last:
                    raise SourceFailure("controllers_changed")
            if (
                binding(json.loads(kube_path.read_text())) != original_binding
                or metrics_binding(load_config(prom_path).model_dump(by_alias=True))
                != metrics_source_sha256
                or OwnershipPolicy.model_validate_json(
                    Path(os.environ["RECOMMENDATION_SOURCE_POLICY"]).read_text()
                )
                != policy
            ):
                raise SourceFailure("source_configuration_changed")
            return SourceData(
                series,
                before,
                after,
                first,
                policy,
                digest(queries),
                digest(
                    {
                        "prometheus": prom.model_dump(exclude={"token", "queries"}),
                        "ownership": policy.model_dump(),
                    }
                ),
                datetime.now(timezone.utc),
                metrics_source_sha256,
            )
        except SourceFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise SourceFailure("source_timeout") from None
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, AttributeError):
            raise SourceFailure("source_unavailable") from None
