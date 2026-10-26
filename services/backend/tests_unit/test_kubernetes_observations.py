from decimal import Decimal

import pytest

from cloudops.features.workloads.kubernetes import Kubernetes, pod_requests, quantity, snapshot
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
