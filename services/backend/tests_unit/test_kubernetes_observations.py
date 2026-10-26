from decimal import Decimal
from types import SimpleNamespace

import pytest

from cloudops.features.workloads.kubernetes import Kubernetes, pod_requests, quantity, snapshot
from cloudops.features.workloads.schemas import WorkloadObservationPayload
from cloudops.shared.errors import DomainError


@pytest.mark.parametrize(
    "value,expected",
    [
        ("100m", ".1"),
        ("32Mi", str(32 * 1024**2)),
        ("1e3", "1000"),
        ("2Gi", str(2 * 1024**3)),
        ("1n", "0.000000001"),
        ("1Ti", str(1024**4)),
    ],
)
def test_resource_quantities(value, expected):
    assert quantity(value) == Decimal(expected)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "not-a-quantity"])
def test_unknown_or_negative_capacity_is_not_treated_as_free(value):
    with pytest.raises(DomainError) as error:
        quantity(value)
    assert error.value.code == "capacity_unproven"


def test_pod_requests_conservatively_include_init_sidecar_pod_level_and_overhead():
    value = pod_requests(
        {
            "containers": [{"resources": {"requests": {"cpu": "10m", "memory": "1Mi"}}}],
            "initContainers": [{"resources": {"requests": {"cpu": "20m", "memory": "2Mi"}}}],
            "resources": {"requests": {"cpu": "100m", "memory": "10Mi"}},
            "overhead": {"cpu": "5m", "memory": "1Mi"},
        }
    )
    assert value == {"cpu": Decimal(".105"), "memory": Decimal(11 * 1024**2)}


def test_baseline_snapshot_excludes_credentials_and_configuration_values():
    value = snapshot(
        {
            "metadata": {"uid": "fixture-uid"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "fixture:1",
                                "env": [{"name": "PASSWORD", "value": "credential-sentinel"}],
                                "envFrom": [{"secretRef": {"name": "secret-sentinel"}}],
                            }
                        ]
                    }
                },
            },
        }
    )
    assert "credential-sentinel" not in repr(value) and "secret-sentinel" not in repr(value)
    assert value["uid"] == "fixture-uid" and "not captured" in value["limitations"]


@pytest.mark.parametrize(
    "case",
    [
        "capacity",
        "placement",
        "host_port",
        "node_pressure",
        "multi_node",
        "pod_limit",
        "extended_resource",
    ],
)
def test_scale_rejects_unproven_single_node_capacity(case):
    kube = object.__new__(Kubernetes)
    node = {
        "metadata": {"name": "fixture-node"},
        "spec": {},
        "status": {
            "conditions": [
                {"type": key, "status": "True" if key == "Ready" else "False"}
                for key in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure")
            ],
            "allocatable": {"cpu": "4", "memory": "4Gi", "pods": "110"},
        },
    }
    spec = {
        "containers": [
            {"name": "app", "resources": {"requests": {"cpu": "600m", "memory": "60Mi"}}}
        ]
    }
    deployment = {"spec": {"replicas": 1, "template": {"spec": spec}}}
    pods = [{"spec": {**spec, "nodeName": "fixture-node"}, "status": {"phase": "Running"}}]
    nodes = [node]
    if case == "capacity":
        node["status"]["allocatable"].update(cpu="1", memory="100Mi")
    if case == "pod_limit":
        node["status"]["allocatable"]["pods"] = "1"
    if case == "extended_resource":
        spec["containers"][0]["resources"]["requests"]["example/gpu"] = "1"
    if case == "placement":
        spec["nodeSelector"] = {"unsupported": "fixture"}
    if case == "host_port":
        spec["containers"][0]["ports"] = [{"hostPort": 80}]
    if case == "node_pressure":
        node["status"]["conditions"][1]["status"] = "True"
    if case == "multi_node":
        nodes = [node, node]
    kube.request = lambda path: {"items": nodes if path.endswith("nodes") else pods}
    with pytest.raises(DomainError) as error:
        kube.capacity(deployment, 2)
    assert error.value.code == "capacity_unproven"


def make_workload(uid="dep-1", namespace="demo", name="checkout"):
    return SimpleNamespace(uid=uid, namespace=namespace, name=name)


def make_template(image="app:1", env=None):
    container = {"name": "app", "image": image}
    if env is not None:
        container["env"] = env
    return {"metadata": {"labels": {"app": "web"}}, "spec": {"containers": [container]}}


def make_deployment(
    uid="dep-1",
    replicas=2,
    generation=3,
    template=None,
    status="default",
    conditions=None,
):
    if status == "default":
        status = {
            "observedGeneration": generation,
            "replicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "updatedReplicas": replicas,
        }
    if conditions is not None:
        status = {**status, "conditions": conditions}
    value = {
        "metadata": {"uid": uid, "generation": generation, "resourceVersion": "42"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": "web"}},
            "template": template or make_template(),
        },
    }
    if status is not None:
        value["status"] = status
    return value


def make_replicaset(uid="rs-1", owner="dep-1", template=None):
    return {
        "metadata": {
            "uid": uid,
            "labels": {"app": "web", "pod-template-hash": "hash-1"},
            "ownerReferences": [{"uid": owner, "kind": "Deployment", "controller": True}],
        },
        "spec": {
            "selector": {"matchLabels": {"app": "web"}},
            "template": template or make_template(),
        },
    }


def make_container(name="app", ready=True, restarts=0, waiting=None, exit_code=None):
    state = {"running": {}}
    last_state = {}
    if waiting is not None:
        state = {"waiting": {"reason": waiting, "message": "provider-message-sentinel"}}
    if exit_code is not None:
        state = {"terminated": {"exitCode": exit_code, "message": "provider-message-sentinel"}}
    return {
        "name": name,
        "ready": ready,
        "restartCount": restarts,
        "state": state,
        "lastState": last_state,
    }


def make_pod(
    uid="pod-1",
    owner="rs-1",
    ready=True,
    restarts=0,
    init_restarts=0,
    phase="Running",
    deletion=None,
    status=True,
    waiting=None,
    exit_code=None,
    scheduled=True,
):
    metadata = {"uid": uid, "name": f"pod-{uid}", "labels": {"app": "web"}}
    if deletion:
        metadata["deletionTimestamp"] = "2026-10-26T00:00:00Z"
    value = {
        "metadata": {
            **metadata,
            "ownerReferences": [{"uid": owner, "kind": "ReplicaSet", "controller": True}],
        },
        "spec": {
            "containers": [{"name": "app", "image": "app:1"}],
            "initContainers": [{"name": "init", "image": "init:1"}],
        },
    }
    if status:
        value["status"] = {
            "phase": phase,
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"},
                {
                    "type": "PodScheduled",
                    "status": "True" if scheduled else "False",
                    **({} if scheduled else {"reason": "Unschedulable"}),
                },
            ],
            "containerStatuses": [
                make_container(ready=ready, restarts=restarts, waiting=waiting, exit_code=exit_code)
            ],
            "initContainerStatuses": [
                make_container(name="init", restarts=init_restarts, exit_code=0)
            ],
        }
    return value


class FakeCluster:
    def __init__(
        self,
        deployment,
        replicasets=(),
        pods=(),
        replicaset_continue=None,
        pod_continue=None,
    ):
        self.deployment = deployment
        self.replicasets = list(replicasets)
        self.pods = list(pods)
        self.replicaset_continue = replicaset_continue
        self.pod_continue = pod_continue

    def __call__(self, path, **kwargs):
        if "/deployments/" in path:
            return self.deployment
        if "/replicasets" in path:
            metadata = {"continue": self.replicaset_continue} if self.replicaset_continue else {}
            return {"metadata": metadata, "items": self.replicasets}
        if "/pods" in path:
            metadata = {"continue": self.pod_continue} if self.pod_continue else {}
            return {"metadata": metadata, "items": self.pods}
        raise AssertionError(f"Unexpected read: {path}")


def observe(cluster, workload=None, previous_payload=None):
    kube = object.__new__(Kubernetes)
    kube.request = cluster
    return kube.observe(workload or make_workload(), previous_payload=previous_payload)


def test_observation_follows_owner_uids_not_labels():
    decoy = make_pod(uid="pod-decoy", owner="rs-other", ready=False)
    payload = observe(
        FakeCluster(
            make_deployment(), [make_replicaset()], [make_pod("pod-1"), make_pod("pod-2"), decoy]
        )
    )
    assert {pod.uid for pod in payload.pods} == {"pod-1", "pod-2"}
    assert payload.coverage.complete
    assert payload.status == "healthy"
    assert payload.ready_containers == payload.total_containers == 2


def test_replacement_uid_pods_do_not_count():
    payload = observe(
        FakeCluster(
            make_deployment(uid="dep-2", replicas=1),
            [make_replicaset(owner="dep-2")],
            [make_pod(owner="rs-old")],
        ),
        workload=make_workload(uid="dep-2"),
    )
    assert payload.pods == []
    assert not payload.coverage.complete
    assert "pods" in payload.coverage.missing_sources
    assert payload.status != "healthy"


@pytest.mark.parametrize(
    "case,missing",
    [
        ("deployment_status", "deployment_status"),
        ("no_replicaset", "current_replicaset"),
        ("ambiguous_replicaset", "current_replicaset"),
        ("template_mismatch", "current_replicaset"),
        ("pod_status", "pod_status"),
        ("container_status", "container_status"),
        ("pagination", "pagination"),
        ("missing_pod", "pods"),
    ],
)
def test_missing_and_ambiguous_sources_are_partial(case, missing):
    deployment = make_deployment()
    replicasets = [make_replicaset()]
    pods = [make_pod("pod-1"), make_pod("pod-2")]
    if case == "deployment_status":
        deployment = make_deployment(status=None)
    elif case == "no_replicaset":
        replicasets = []
    elif case == "ambiguous_replicaset":
        replicasets = [make_replicaset("rs-1"), make_replicaset("rs-2")]
    elif case == "template_mismatch":
        replicasets = [make_replicaset(template=make_template(image="other:2"))]
    elif case == "pod_status":
        pods = [make_pod("pod-1"), make_pod("pod-2", status=False)]
    elif case == "container_status":
        pod = make_pod("pod-2")
        pod["status"]["containerStatuses"] = []
        pods = [make_pod("pod-1"), pod]
    elif case == "missing_pod":
        pods = [make_pod("pod-1")]
    payload = observe(
        FakeCluster(
            deployment,
            replicasets,
            pods,
            pod_continue="next" if case == "pagination" else None,
        )
    )
    assert not payload.coverage.complete
    assert missing in payload.coverage.missing_sources
    assert payload.status != "healthy"


def test_current_replicaset_requires_template_match():
    payload = observe(
        FakeCluster(
            make_deployment(),
            [make_replicaset(template=make_template(image="other:2"))],
            [make_pod("pod-1"), make_pod("pod-2")],
        )
    )
    assert "current_replicaset" in payload.coverage.missing_sources
    assert payload.status != "healthy"


def test_terminating_terminal_and_old_pods_do_not_count_as_ready():
    pods = [
        make_pod("pod-1", ready=False, deletion=True),
        make_pod("pod-2", ready=False, phase="Succeeded"),
        make_pod("pod-3", ready=False, owner="rs-old"),
        make_pod("pod-4", ready=False),
    ]
    payload = observe(FakeCluster(make_deployment(), [make_replicaset()], pods))
    assert {pod.uid for pod in payload.pods} == {"pod-4"}
    assert payload.ready_containers == 0
    assert payload.status != "healthy"


def test_regular_and_init_container_restarts_are_identity_scoped():
    payload = observe(
        FakeCluster(
            make_deployment(replicas=1),
            [make_replicaset()],
            [make_pod("pod-1", restarts=2, init_restarts=3)],
        )
    )
    assert payload.restart_total == 5
    init = next(container for container in payload.pods[0].containers if container.init)
    assert init.restart_count == 3 and init.ready is True
    assert payload.status == "healthy"


def test_restart_delta_requires_matching_pod_and_container():
    previous = WorkloadObservationPayload.model_validate(
        observe(
            FakeCluster(
                make_deployment(replicas=1), [make_replicaset()], [make_pod("pod-old", restarts=5)]
            )
        ).model_dump()
    )
    replacement = observe(
        FakeCluster(
            make_deployment(replicas=1), [make_replicaset()], [make_pod("pod-new", restarts=5)]
        ),
        previous_payload=previous,
    )
    assert replacement.restart_total == 5
    assert replacement.restart_delta == 0
    same_pod = observe(
        FakeCluster(
            make_deployment(replicas=1), [make_replicaset()], [make_pod("pod-old", restarts=7)]
        ),
        previous_payload=previous,
    )
    assert same_pod.restart_delta == 2


def test_zero_replicas_is_scaled_to_zero():
    payload = observe(FakeCluster(make_deployment(replicas=0, status={}), [make_replicaset()], []))
    assert payload.status == "scaled_to_zero"
    assert payload.coverage.complete
    assert payload.desired_replicas == 0


@pytest.mark.parametrize(
    "case,expected",
    [
        ("healthy", "healthy"),
        ("unready", "progressing"),
        ("progress_deadline", "degraded"),
        ("crash_loop", "degraded"),
        ("image_pull", "degraded"),
        ("nonzero_exit", "degraded"),
        ("unschedulable", "degraded"),
    ],
)
def test_rollout_health_classifies_failures_and_progressing(case, expected):
    deployment = make_deployment(replicas=1)
    pods = [make_pod("pod-1")]
    if case == "unready":
        deployment = make_deployment(
            replicas=1,
            status={
                "observedGeneration": 3,
                "replicas": 1,
                "readyReplicas": 0,
                "availableReplicas": 0,
                "updatedReplicas": 1,
            },
        )
        pods = [make_pod("pod-1", ready=False)]
    elif case == "progress_deadline":
        deployment = make_deployment(
            replicas=1,
            status={
                "observedGeneration": 3,
                "replicas": 1,
                "readyReplicas": 0,
                "availableReplicas": 0,
                "updatedReplicas": 1,
                "conditions": [
                    {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
                ],
            },
        )
        pods = [make_pod("pod-1", ready=False)]
    elif case == "crash_loop":
        pods = [make_pod("pod-1", ready=False, waiting="CrashLoopBackOff")]
    elif case == "image_pull":
        pods = [make_pod("pod-1", ready=False, waiting="ImagePullBackOff")]
    elif case == "nonzero_exit":
        pods = [make_pod("pod-1", ready=False, exit_code=1)]
    elif case == "unschedulable":
        pods = [make_pod("pod-1", ready=False, scheduled=False)]
    payload = observe(FakeCluster(deployment, [make_replicaset()], pods))
    assert payload.status == expected
    if expected == "degraded":
        assert payload.reasons


def test_direct_payload_excludes_literals_and_provider_messages():
    template = {
        "metadata": {"labels": {"app": "web"}},
        "spec": {
            "serviceAccountName": "app-account",
            "containers": [
                {
                    "name": "app",
                    "image": "app:1",
                    "env": [
                        {"name": "PASSWORD", "value": "env-literal-sentinel"},
                        {
                            "name": "TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "app-secret", "key": "token"}},
                        },
                    ],
                    "envFrom": [{"configMapRef": {"name": "app-config"}}],
                }
            ],
            "volumes": [{"name": "data", "secret": {"secretName": "volume-secret"}}],
        },
    }
    pod = make_pod("pod-1", waiting="CrashLoopBackOff")
    pod["status"]["message"] = "provider-message-sentinel"
    payload = observe(
        FakeCluster(
            make_deployment(template=template, replicas=1),
            [make_replicaset(template=template)],
            [pod],
        )
    )
    rendered = repr(payload.model_dump())
    assert "env-literal-sentinel" not in rendered
    assert "provider-message-sentinel" not in rendered
    assert payload.template_fields["containers"][0]["env"][0] == "PASSWORD"
    references = {(item.kind, item.name) for item in payload.config_references}
    assert ("secret", "app-secret") in references
    assert ("config_map", "app-config") in references
    assert ("secret", "volume-secret") in references
    assert ("service_account", "app-account") in references
    assert payload.application_telemetry == "unverified"
