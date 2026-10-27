"""Controlled Kubernetes objects. These fixtures are not live cluster evidence."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from cloudops.features.workloads.observation_evaluator import evaluate
from cloudops.features.workloads.observation_schemas import Identity


def objects():
    template = {
        "metadata": {"labels": {"app": "checkout"}},
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "image": "fixture:1",
                    "resources": {"requests": {"cpu": "100m"}},
                    "env": [{"name": "TOKEN", "value": "private-env-sentinel"}],
                    "args": ["private-args-sentinel"],
                }
            ]
        },
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout",
            "namespace": "demo",
            "uid": "deploy-uid",
            "generation": 1,
            "resourceVersion": "opaque-d1",
            "annotations": {"private": "private-annotation-sentinel"},
        },
        "spec": {"replicas": 1, "template": template},
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    rs = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "namespace": "demo",
            "name": "checkout-rs",
            "uid": "rs-uid",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "uid": "deploy-uid",
                    "controller": True,
                }
            ],
        },
        "spec": {"template": deepcopy(template)},
    }
    rs["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = "controller-hash"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "namespace": "demo",
            "name": "checkout-pod",
            "uid": "pod-uid",
            "labels": {"app": "checkout"},
            "ownerReferences": [
                {"apiVersion": "apps/v1", "kind": "ReplicaSet", "uid": "rs-uid", "controller": True}
            ],
        },
        "spec": deepcopy(template["spec"]),
        "status": {
            "phase": "Running",
            "message": "private-pod-sentinel",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": True,
                    "started": True,
                    "restartCount": 0,
                    "state": {"running": {}},
                    "lastState": {
                        "terminated": {"exitCode": 1, "message": "private-termination-sentinel"}
                    },
                }
            ],
        },
    }
    return (
        deployment,
        {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSetList",
            "metadata": {"resourceVersion": "opaque-r"},
            "items": [rs],
        },
        {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": {"resourceVersion": "opaque-p"},
            "items": [pod],
        },
    )


def observed(identity=None, started_at=None, data=None):
    identity = identity or Identity(
        workload_id=uuid4(),
        cluster="lab",
        namespace="demo",
        name="checkout",
        uid="deploy-uid",
        epoch=0,
    )
    start = started_at or datetime.now(timezone.utc)
    deployment, rs, pods = data or objects()
    return evaluate(identity, deployment, deepcopy(deployment), rs, pods, start, start)
