"""Deterministic E04 integration fixtures using the real producer adapters.

Kubernetes adapters receive a controlled client through their single construction
path; Prometheus reads go through the real bounded client with a controlled HTTP
transport. The fixture remains explicitly synthetic.
"""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from cloudops.features.investigations import collector, kubernetes_collector, prometheus
from cloudops.features.investigations.prometheus_config import QUERY_IDS, PrometheusConfig
from cloudops.features.workloads.observation_schemas import (
    DeploymentObservation,
    Identity,
    Observation,
)

CERT = Path(__file__).resolve().parent / "fixtures/scope-ca.crt"
WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(minutes=30)
INJECTOR_CANARY = "injector-truth-canary"
INJECTOR_SECRET = "injector-secret-canary"
KUBERNETES_KEYS = (
    "kubernetes.state",
    "kubernetes.events",
    "kubernetes.logs.current",
    "kubernetes.logs.previous",
    "kubernetes.configmap.metadata",
)
CALLS: dict[str, int] = {}


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


def _labels(context) -> dict:
    return {
        "namespace": context.namespace,
        "deployment_uid": context.workload_uid,
        "pod": f"{context.scope_snapshot.name}-fixture",
        "container": "app",
    }


class FakeKubernetes:
    """Controlled Kubernetes reader for one scenario; not a live cluster claim."""

    truncated = False

    def __init__(self, scenario: str = "healthy"):
        self.scenario = scenario

    def _pod(self, namespace: str, name: str, index: int) -> dict:
        uid = f"pod-{index}"
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "uid": uid,
                "name": f"{name}-{index}",
                "namespace": namespace,
                "labels": {"app": name},
                "ownerReferences": [
                    {
                        "uid": "rs-1",
                        "kind": "ReplicaSet",
                        "apiVersion": "apps/v1",
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "fixture:1",
                        "resources": {"requests": {"cpu": "100m"}},
                    }
                ],
                "nodeName": "fixture-node",
            },
        }
        if self.scenario == "injector":
            pod["metadata"]["annotations"] = {
                "cloudops.io/injector-truth": INJECTOR_CANARY,
                "kubectl.kubernetes.io/restartedAt": "2026-01-01T00:00:00Z",
            }
            pod["spec"]["containers"][0]["env"] = [
                {"name": "INJECTOR_TRUTH", "value": INJECTOR_CANARY},
                {
                    "name": "TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": "injector-secret", "key": "token"}},
                },
            ]
        if self.scenario == "pending":
            pod["spec"].pop("nodeName")
            pod["status"] = {
                "phase": "Pending",
                "conditions": [
                    {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
                ],
            }
            return pod
        if self.scenario == "imagepull":
            pod["status"] = {
                "phase": "Pending",
                "conditions": [{"type": "PodScheduled", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 0,
                        "state": {"waiting": {"reason": "ImagePullBackOff"}},
                    }
                ],
            }
            return pod
        if self.scenario == "crashloop":
            pod["status"] = {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 3,
                        "started": True,
                        "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                    }
                ],
            }
            return pod
        if self.scenario == "oom":
            pod["status"] = {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 2,
                        "started": True,
                        "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                    }
                ],
            }
            return pod
        pod["status"] = {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 0,
                    "started": True,
                    "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                }
            ],
        }
        return pod

    def _pods(self, namespace: str, name: str) -> list[dict]:
        return [self._pod(namespace, name, index) for index in range(2)]

    def get_deployment(self, namespace: str, name: str, expected_uid: str) -> dict:
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "uid": expected_uid,
                "namespace": namespace,
                "name": name,
                "generation": 1,
                "resourceVersion": "rv-1",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
            },
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "fixture:1",
                                "resources": {"requests": {"cpu": "100m"}},
                                "envFrom": [{"configMapRef": {"name": "checkout-config"}}],
                            }
                        ]
                    }
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        }
        if self.scenario == "injector":
            deployment["metadata"]["annotations"]["cloudops.io/injector-truth"] = INJECTOR_CANARY
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            container["env"] = [
                {"name": "INJECTOR_TRUTH", "value": INJECTOR_CANARY},
                {
                    "name": "TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": "injector-secret", "key": "token"}},
                },
            ]
        return deployment

    def list_owned_replicasets(self, namespace: str, deployment_uid: str) -> list[dict]:
        return [
            {
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "metadata": {
                    "uid": "rs-1",
                    "name": "fixture-rs",
                    "namespace": namespace,
                    "annotations": {"deployment.kubernetes.io/revision": "1"},
                    "ownerReferences": [
                        {
                            "uid": deployment_uid,
                            "kind": "Deployment",
                            "apiVersion": "apps/v1",
                            "controller": True,
                        }
                    ],
                },
            }
        ]

    def list_owned_pods(self, namespace: str, replicaset_uids: set[str]) -> list[dict]:
        name = "fixture"
        return self._pods(namespace, name)

    def get_pod(
        self, namespace: str, name: str, expected_uid: str, replicaset_uids: set[str]
    ) -> dict:
        return next(
            pod for pod in self._pods(namespace, "fixture") if pod["metadata"]["name"] == name
        )

    def list_namespace_events(self, namespace: str, involved_uids: set[str]) -> list[dict]:
        reason = {
            "crashloop": "BackOff",
            "oom": "OOMKilling",
            "pending": "FailedScheduling",
            "imagepull": "ImagePullBackOff",
        }.get(self.scenario, "ScalingReplicaSet")
        pod = self._pods(namespace, "fixture")[0]
        message = {
            "crashloop": "Back-off restarting failed container",
            "oom": "Container killed due to memory limit",
            "pending": "0/1 nodes are available: insufficient cpu",
            "imagepull": "Failed to pull image fixture:1",
        }.get(self.scenario, "Scaled up replica set")
        return [
            {
                "metadata": {
                    "uid": "event-1",
                    "name": "fixture.1",
                    "namespace": namespace,
                },
                "involvedObject": {
                    "uid": pod["metadata"]["uid"],
                    "kind": "Pod",
                    "namespace": namespace,
                },
                "reason": reason,
                "message": message,
                "type": "Warning" if self.scenario != "healthy" else "Normal",
                "count": 1,
                "lastTimestamp": "2026-01-01T00:05:00Z",
            }
        ]

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: str,
        previous: bool = False,
        since_time: str | None = None,
        line_limit: int = 200,
    ) -> tuple[str, bool]:
        text = (
            "2026-01-01T00:05:00Z crash line\n"
            if self.scenario in {"crashloop", "oom"}
            else "2026-01-01T00:05:00Z healthy line\n"
        )
        return text, False

    def get_configmap_metadata(self, namespace: str, name: str) -> dict:
        metadata = {
            "uid": "cm-1",
            "name": name,
            "namespace": namespace,
            "resourceVersion": "cm-rv-1",
        }
        value = {
            "kind": "PartialObjectMetadata",
            "apiVersion": "meta.k8s.io/v1",
            "metadata": metadata,
        }
        if self.scenario == "injector":
            metadata["annotations"] = {"cloudops.io/injector-truth": INJECTOR_CANARY}
            value["data"] = {"injector-truth": INJECTOR_CANARY}
        return value


def _config() -> PrometheusConfig:
    query = (
        "sum by (namespace, deployment_uid) (cloudops_metric{"
        'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
    )
    return PrometheusConfig.model_validate(
        {
            "schema": "PrometheusReadConfig/v1",
            "endpoint_id": "e04-fixture",
            "producer_version": "prometheus/v3.5.0",
            "server": "https://prometheus.test:8443/prometheus",
            "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
            "step_seconds": 30,
            "binding": {
                "deployment_uid_label": "deployment_uid",
                "namespace_label": "namespace",
            },
            "queries": {query_id: {"query": query, "unit": "count"} for query_id in QUERY_IDS},
        }
    )


def _metric_payload(context, query_id: str) -> dict:
    start = context.immutable_window.start.timestamp()
    end = context.immutable_window.end.timestamp()
    values = []
    moment = start
    while moment <= end + 0.001:
        values.append([moment, "12"])
        moment += 30
    if values[-1][0] < end:
        values.append([end, "12"])
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": _labels(context), "values": values}],
        },
    }


def _prometheus_adapter(config, ssl_context, query_id: str):
    def read(context, timeout):
        CALLS[f"prometheus.{query_id}"] = CALLS.get(f"prometheus.{query_id}", 0) + 1
        body = json.dumps(_metric_payload(context, query_id)).encode()
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Stream(body)))
        return asyncio.run(
            prometheus.read_query(config, ssl_context, query_id, context, timeout, transport)
        )

    return read


def binding(monkeypatch, scenario: str = "healthy") -> dict:
    """Install real K8s/Prometheus adapters and return the 12-key binding."""
    fake = FakeKubernetes(scenario)
    monkeypatch.setattr(kubernetes_collector, "_scoped_client", lambda _deadline: fake)
    config = _config()
    ssl_context = config.ssl_context()
    result = {key: collector.REGISTERED_ADAPTERS[key] for key in KUBERNETES_KEYS}
    for query_id in QUERY_IDS:
        key = f"prometheus.{query_id}"
        adapter = _prometheus_adapter(config, ssl_context, query_id)
        monkeypatch.setitem(collector.REGISTERED_ADAPTERS, key, adapter)
        result[key] = adapter
    result["development_fixture"] = collector.REGISTERED_ADAPTERS["development_fixture"]
    return result


def observation(identity: Identity, moment: datetime) -> Observation:
    return Observation(
        observation_id=uuid4(),
        identity=identity,
        started_at=moment,
        observed_at=moment,
        deployment=DeploymentObservation(
            generation=1,
            resource_version="rv-1",
            end_resource_version="rv-1",
            observed_generation=1,
            desired=1,
            ready=1,
            available=1,
            updated=1,
            replicas=1,
        ),
        replicaset_resource_version="rv-1",
        pod_resource_version="rv-1",
        current_replicaset_uid="rs-1",
        coverage="complete",
        health="healthy",
        reasons=[],
        pods=[],
        unrelated_pods=0,
        template_fields=[],
        config_references=[],
    )
