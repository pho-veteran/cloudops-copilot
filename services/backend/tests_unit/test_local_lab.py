"""Owned-lab guards and measured fixtures, without Docker, Kubernetes or PostgreSQL."""

from collections import Counter
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import shutil
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    not (ROOT / "scripts/manage-local-lab.py").is_file(),
    reason="Host lab tooling is tested from a checkout; runtime images omit host scripts.",
)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def driver(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    value = module(ROOT / "scripts/manage-local-lab.py", "owned_lab_test")
    monkeypatch.setattr(value, "DIRECTORY", tmp_path / "lab")
    return value


@pytest.fixture
def lab(driver, monkeypatch):
    value = driver.Lab(driver.DIRECTORY)
    value.directory.mkdir(mode=0o700)
    value.marker = {
        "project": driver.OWNED_PROJECT,
        "root": str(ROOT),
        "owner": str(uuid4()),
        "deployments": {"checkout": "original", "history": "history-uid"},
    }
    value.save()
    monkeypatch.setattr(value, "resources", lambda: {"container": [], "volume": [], "network": []})
    return value


def deployment():
    return {
        "metadata": {
            "name": "checkout",
            "namespace": "demo",
            "uid": "original",
            "resourceVersion": "opaque",
            "annotations": {"keep": "value"},
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "owned",
                            "imagePullPolicy": "Never",
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "400m", "memory": "160Mi"},
                            },
                            "env": [{"name": "KEEP", "value": "preserved"}],
                        }
                    ]
                }
            },
        },
    }


def test_missing_marker_never_adopts_same_named_project(driver, monkeypatch):
    lab = driver.Lab(driver.DIRECTORY)
    monkeypatch.setattr(lab, "resources", lambda: {"volume": [{"Name": "same-name"}]})
    with pytest.raises(RuntimeError, match="adopt"):
        lab.ownership(allow_new=True)
    assert not lab.directory.exists()


@pytest.mark.parametrize("kind", ["volume", "network"])
def test_unlabelled_same_named_data_resource_is_rejected_before_creation(driver, monkeypatch, kind):
    value = driver.Lab(driver.DIRECTORY)
    name = driver.OWNED_PROJECT + ("_database" if kind == "volume" else "_default")

    def command(args):
        if args[1:3] == [kind, "inspect"]:
            return json.dumps([{"Name": name, "Labels": {}}])
        if args[1] == kind and args[-1] == f"name=^{driver.OWNED_PROJECT}_":
            return name
        return ""

    monkeypatch.setattr(value, "command", command)
    with pytest.raises(RuntimeError, match="adopt"):
        value.ownership(allow_new=True)
    assert not value.directory.exists()


def test_preflight_accepts_builtin_networks_without_ipam_allocations(lab, driver, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(
        driver.socket,
        "socket",
        lambda: nullcontext(type("Socket", (), {"bind": lambda self, address: None})()),
    )
    info = {
        "ServerVersion": "29.9.2",
        "OSType": "linux",
        "Architecture": "x86_64",
        "CgroupVersion": "2",
        "MemoryLimit": True,
        "SwapLimit": True,
        "NCPU": 12,
        "MemTotal": 14 * driver.GIB,
    }

    def command(args):
        if args[:2] == ["docker", "info"]:
            return json.dumps(info)
        if args[:2] == ["ip", "-j"]:
            return "[]"
        if args == ["docker", "network", "ls", "-q"]:
            return "host none"
        assert args == ["docker", "network", "inspect", "host", "none"]
        return json.dumps([{"IPAM": {"Config": None}}, {"IPAM": None}])

    monkeypatch.setattr(lab, "command", command)
    monkeypatch.setattr(
        driver.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 30 * driver.GIB})()
    )
    assert lab.preflight()["docker"] == "29.9.2"


@pytest.mark.parametrize("kind", ["container", "volume", "network"])
def test_owner_mismatch_blocks_every_resource_kind(lab, driver, monkeypatch, kind):
    item = {
        "Config": {"Labels": {driver.OWNER_LABEL: "foreign"}},
        "Labels": {driver.OWNER_LABEL: "foreign"},
    }
    monkeypatch.setattr(lab, "resources", lambda: {kind: [item]})
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        lab.ownership()


def test_marker_cannot_be_copied_from_other_checkout(lab):
    lab.marker["root"] = "/another/checkout"
    lab.save()
    with pytest.raises(RuntimeError, match="checkout"):
        lab.ownership()


@pytest.mark.parametrize("mutation", ["anonymous", "extra_bind", "missing"])
def test_k3s_mount_inventory_refuses_unowned_or_missing_storage(lab, driver, monkeypatch, mutation):
    expected = {
        "/var/lib/rancher/k3s": "k3s_data",
        "/var/log": "k3s_logs",
        "/var/lib/cni": "k3s_cni",
        "/var/lib/kubelet": "k3s_kubelet",
    }
    mounts = [
        {"Type": "volume", "Destination": path, "Name": driver.OWNED_PROJECT + "_" + name}
        for path, name in expected.items()
    ]
    if mutation == "anonymous":
        mounts[0]["Name"] = "unlabelled-anonymous-volume"
    elif mutation == "extra_bind":
        mounts.append({"Type": "bind", "Destination": "/host", "Source": "/"})
    else:
        mounts.pop()
    item = {
        "Config": {
            "Labels": {
                driver.OWNER_LABEL: lab.marker["owner"],
                "com.docker.compose.project": driver.OWNED_PROJECT,
                "com.docker.compose.service": "k3s",
            }
        },
        "Mounts": mounts,
    }
    monkeypatch.setattr(lab, "resources", lambda: {"container": [item]})
    with pytest.raises(RuntimeError, match="Unexpected K3s mount"):
        lab.ownership()


def test_generated_file_is_private_and_symlink_refused(lab, driver):
    target = lab.directory / "credential"
    driver.private_write(target, "synthetic-only")
    assert target.stat().st_mode & 0o777 == 0o600
    target.unlink()
    outside = lab.directory / "outside"
    outside.write_text("preserve")
    target.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlinks"):
        driver.private_write(target, "replace")
    assert outside.read_text() == "preserve"


def reader_connection():
    return {
        "server": "https://k3s:6443",
        "ca_data": base64.b64encode(
            (ROOT / "services/backend/tests/fixtures/scope-ca.crt").read_bytes()
        ).decode(),
        "token": "synthetic-test-only",
    }


@pytest.mark.parametrize("changed", [None, "image", "ca"])
def test_resume_reuses_artifacts_and_keeps_optional_load_stopped(lab, driver, monkeypatch, changed):
    from cloudops.features.workloads.scope_config import cluster_fingerprint

    config = reader_connection()
    lab.marker.update(
        sample_image=driver.SAMPLE_IMAGE,
        namespace_uid="namespace",
        cluster_fingerprint=cluster_fingerprint(config),
    )
    calls = []
    names = ("db", "k3s", *driver.APP_SERVICES, "https", "prometheus", "alertmanager")

    def dc(*args, **kwargs):
        calls.append(args)
        return (
            json.dumps({"services": {name: {"image": name} for name in names}})
            if args[0] == "config"
            else ""
        )

    monkeypatch.setattr(lab, "dc", dc)
    monkeypatch.setattr(lab, "volume_guard", lambda: None)
    monkeypatch.setattr(lab, "credentials_valid", lambda: None)
    monkeypatch.setattr(lab, "service", lambda name: {"Image": "retained"})
    monkeypatch.setattr(
        lab,
        "command",
        lambda args: json.dumps(
            [
                {
                    "Id": "different" if changed == "image" else "retained",
                    "Os": "linux",
                    "Architecture": "amd64",
                }
            ]
        ),
    )
    monkeypatch.setattr(lab, "start_k3s", lambda **kwargs: calls.append(("node", kwargs)))
    monkeypatch.setattr(lab, "get", lambda *args: {"metadata": {"uid": "namespace"}})
    monkeypatch.setattr(lab, "deployment", lambda name: None)
    monkeypatch.setattr(lab, "kube", lambda *args: config["ca_data"])
    if changed == "ca":
        lab.marker["cluster_fingerprint"] = "0" * 64
    monkeypatch.setattr(lab, "dispatch", lambda *args: calls.append(("dispatch", *args)))
    monkeypatch.setattr(lab, "maintenance", lambda *args: None)
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "namespace_check", lambda: None)
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    if changed:
        with pytest.raises(
            RuntimeError, match="image changed" if changed == "image" else "identity changed"
        ):
            lab.resume()
    else:
        lab.resume()
        assert ("node", {"pull_missing": False}) in calls
        assert not any(args[0] == "build" or args[-1] in ("load", "krr") for args in calls)
        assert all(
            args[args.index("--pull") + 1] == "never" and "--no-build" in args
            for args in calls
            if args[0] == "up"
        )
        assert not any("recovery_execution" in args for args in calls)


def test_reader_scope_pins_cluster_once_and_has_names_separate_from_uid_lists(lab):
    config = reader_connection()
    scope = lab.reader_scope(config)
    assert scope["schema_version"] == 2 and scope["cluster"]["key"] == "local-k3s"
    assert scope["deployments"] == [
        {"namespace": "demo", "name": "checkout"},
        {"namespace": "demo", "name": "history"},
    ]
    assert lab.reader_scope({**config, "token": "different-synthetic-token"}) == scope
    pin = lab.marker["cluster_fingerprint"]
    with pytest.raises(RuntimeError, match="cannot repin"):
        lab.reader_scope({**config, "server": "https://another-cluster:6443"})
    assert lab.marker["cluster_fingerprint"] == pin


@pytest.mark.parametrize("source", ["client", "scope"])
def test_reader_scope_cannot_replace_existing_fingerprint_when_marker_is_unpinned(lab, source):
    folder = lab.directory / "reader"
    folder.mkdir()
    config = reader_connection()
    if source == "client":
        (folder / "client.json").write_text(json.dumps(config))
    else:
        scope = lab.reader_scope(config)
        (folder / "scope.json").write_text(json.dumps(scope))
        lab.marker.pop("cluster_fingerprint")
        lab.save()
    with pytest.raises(RuntimeError, match="cannot repin"):
        lab.reader_scope({**config, "server": "https://another-cluster:6443"})
    assert "cluster_fingerprint" not in lab.marker


@pytest.mark.parametrize("mismatch", ["names", "uid", "fingerprint"])
def test_credential_validation_checks_v2_policy_and_legacy_uid_lists(lab, mismatch):
    config = reader_connection()
    policy = lab.reader_scope(config)
    for kind in ["reader", "writer", "monitoring"]:
        folder = lab.directory / kind
        folder.mkdir()
        (folder / "expiry.json").write_text(json.dumps({"expires_at": "2099-01-01T00:00:00Z"}))
        if kind != "monitoring":
            rows = [
                {
                    "cluster": "local-k3s",
                    "namespace": "demo",
                    "name": name,
                    "uid": uid,
                    "source": "live_kubernetes",
                }
                for name, uid in lab.marker["deployments"].items()
                if kind == "reader" or name == "checkout"
            ]
            (folder / "workloads.json").write_text(json.dumps(rows))
    (lab.directory / "reader/client.json").write_text(json.dumps(config))
    (lab.directory / "reader/scope.json").write_text(json.dumps(policy))
    lab.credentials_valid()
    if mismatch == "uid":
        path = lab.directory / "reader/workloads.json"
        rows = json.loads(path.read_text())
        rows[0]["uid"] = "replacement"
        path.write_text(json.dumps(rows))
    else:
        if mismatch == "names":
            policy["deployments"].pop()
        else:
            policy["cluster"]["expected_fingerprint"] = "0" * 64
        (lab.directory / "reader/scope.json").write_text(json.dumps(policy))
    with pytest.raises(RuntimeError, match="scope differs"):
        lab.credentials_valid()


def test_node_bounds_are_reapplied_and_effectively_checked(lab, driver, monkeypatch):
    node = {
        "Id": "owned-node",
        "HostConfig": {
            "Memory": 4 * driver.GIB,
            "MemorySwap": 4 * driver.GIB,
            "NanoCpus": 4_000_000_000,
        },
        "State": {"Pid": 123},
    }
    calls = []
    monkeypatch.setattr(lab, "ownership", lambda: None)
    monkeypatch.setattr(lab, "service", lambda name: node)
    monkeypatch.setattr(lab, "command", lambda args: calls.append(args))
    monkeypatch.setattr(driver, "process_cgroup", lambda pid: Path("/owned-node/init"))
    monkeypatch.setattr(
        driver, "container_cgroup", lambda group, identity: calls.append((group, identity))
    )
    lab.bound_node()
    assert calls == [
        ["docker", "update", "--memory", "4g", "--memory-swap", "4g", "--cpus", "4", "owned-node"],
        (Path("/owned-node/init"), "owned-node"),
    ]


def test_driver_uses_private_local_docker_configuration(driver, monkeypatch):
    monkeypatch.setenv("LAB_SAMPLE_IMAGE", "unrelated-image")
    for key in (
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "BUILDX_CONFIG",
        "BUILDX_BUILDER",
        "BUILDKIT_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS",
    ):
        monkeypatch.setenv(key, "foreign")
    value = driver.Lab(driver.DIRECTORY)
    assert value.env["DOCKER_CONFIG"] == str(driver.DIRECTORY / "docker-config")
    assert value.env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "LAB_SAMPLE_IMAGE" not in value.env
    assert not any(
        key in value.env
        for key in (
            "DOCKER_CONTEXT",
            "BUILDX_CONFIG",
            "BUILDX_BUILDER",
            "BUILDKIT_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS",
        )
    )


@pytest.mark.parametrize("result", ["verified", "mismatch", "failure"])
def test_registry_inspection_requires_success_and_exact_pinned_digest(lab, monkeypatch, result):
    reference = "postgres:17.12-bookworm@sha256:approved"
    monkeypatch.setattr(
        lab, "dc", lambda *args: json.dumps({"services": {"db": {"image": reference}}})
    )
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if result == "failure":
            raise RuntimeError("registry unavailable")
        return json.dumps(
            {
                "digest": (args[-1].rsplit("@", 1)[1] if "@" in args[-1] else "sha256:resolved")
                if result == "verified"
                else "sha256:wrong"
            }
        )

    monkeypatch.setattr(lab, "command", command)
    if result == "verified":
        assert lab.images()["remote_references_verified"][reference] == "sha256:approved"
    else:
        with pytest.raises(RuntimeError):
            lab.images()
    assert all(call[:4] == ["docker", "buildx", "imagetools", "inspect"] for call in calls)
    config = lab.directory / "docker-config/config.json"
    assert json.loads(config.read_text()) == {}
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("local_digest", ["sha256:approved", "sha256:wrong"])
def test_registry_receipt_reuse_requires_exact_local_pinned_artifact(
    lab, monkeypatch, local_digest
):
    import hashlib

    reference = "postgres:17.12-bookworm@sha256:approved"
    receipt = (
        lab.directory
        / "receipts"
        / ("registry-" + hashlib.sha256(reference.encode()).hexdigest() + ".json")
    )
    receipt.parent.mkdir()
    receipt.write_text(
        json.dumps(
            {
                "reference": reference,
                "digest": "sha256:approved",
                "verified_at": "2026-11-14T00:00:00Z",
            }
        )
    )
    monkeypatch.setattr(
        lab, "dc", lambda *args: json.dumps({"services": {"db": {"image": reference}}})
    )
    inspected = []

    def command(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Os": "linux",
                        "Architecture": "amd64",
                        "RepoDigests": ["postgres@" + local_digest],
                    }
                ]
            )
        inspected.append(args[-1])
        if args[-1] == reference:
            raise RuntimeError("registry unavailable")
        return json.dumps(
            {"digest": args[-1].rsplit("@", 1)[1] if "@" in args[-1] else "sha256:resolved"}
        )

    monkeypatch.setattr(lab, "command", command)
    if local_digest == "sha256:approved":
        assert lab.images()["remote_references_verified"][reference] == local_digest
        assert reference not in inspected
        assert json.loads(receipt.read_text())["verified_at"] == "2026-11-14T00:00:00Z"
    else:
        with pytest.raises(RuntimeError, match="registry unavailable"):
            lab.images()


def test_stale_uid_is_rejected_before_patch(lab, driver, monkeypatch):
    live = deployment()
    live["metadata"]["uid"] = "replacement"
    calls = []
    monkeypatch.setattr(lab, "kube", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="UID changed"):
        lab.patch(live, [])
    assert calls == []


def test_patch_carries_uid_and_opaque_version(lab, monkeypatch):
    calls = []
    monkeypatch.setattr(lab, "kube", lambda *args, **kwargs: calls.append(args))
    lab.patch(deployment(), [{"op": "replace", "path": "/spec/replicas", "value": 2}])
    assert json.loads(calls[0][-1])[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "original"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "opaque"},
    ]


def test_reset_preserves_unrelated_env_annotations_and_does_not_rewrite_authority(
    lab, driver, monkeypatch
):
    before = deployment()
    live = copy.deepcopy(before)
    live["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "LAB_MODE", "value": "slow"}
    )
    expected = driver.owned_fields(live)
    record = {"uid": "original", "before": driver.owned_fields(before), "expected": expected}
    (lab.directory / "fault.json").write_text(json.dumps(record))
    (lab.directory / "writer").mkdir(mode=0o700)
    sentinel = lab.directory / "writer/scope"
    sentinel.write_text("unchanged")
    live["metadata"]["annotations"]["new"] = "keep too"
    live["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "ADDED", "value": "also keep"}
    )
    patches = []
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    restored = copy.deepcopy(live)
    restored["spec"]["template"]["spec"]["containers"][0]["env"] = [
        entry
        for entry in restored["spec"]["template"]["spec"]["containers"][0]["env"]
        if entry["name"] != "LAB_MODE"
    ]
    reads = iter([live, restored])
    monkeypatch.setattr(lab, "deployment", lambda name: next(reads))
    monkeypatch.setattr(lab, "patch", lambda resource, changes: patches.extend(changes))
    monkeypatch.setattr(lab, "kube", lambda *args, **kwargs: "")
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    lab.reset()
    env = next(change["value"] for change in patches if change["path"].endswith("/env"))
    assert env == [{"name": "KEEP", "value": "preserved"}, {"name": "ADDED", "value": "also keep"}]
    assert not any("annotations" in change["path"] for change in patches)
    assert sentinel.read_text() == "unchanged"
    assert not (lab.directory / "fault.json").exists()


def test_unknown_execution_blocks_reset_before_mutation(lab, monkeypatch):
    def denied():
        raise RuntimeError("unresolved execution")

    monkeypatch.setattr(lab, "no_execution", denied)
    monkeypatch.setattr(lab, "patch", lambda *args: pytest.fail("Unexpected write"))
    with pytest.raises(RuntimeError, match="unresolved"):
        lab.reset()


def test_concurrent_owned_field_change_blocks_reset(lab, driver, monkeypatch):
    live = deployment()
    expected = driver.owned_fields(live)
    (lab.directory / "fault.json").write_text(
        json.dumps({"uid": "original", "expected": expected, "before": expected})
    )
    live["spec"]["template"]["spec"]["containers"][0]["image"] = "changed-by-someone-else"
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "deployment", lambda name: live)
    monkeypatch.setattr(lab, "patch", lambda *args: pytest.fail("Unexpected write"))
    with pytest.raises(RuntimeError, match="fields changed"):
        lab.reset()


def test_containment_failure_blocks_fault_before_record_or_patch(lab, monkeypatch):
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(
        lab, "containment", lambda: (_ for _ in ()).throw(RuntimeError("escaped cgroup"))
    )
    monkeypatch.setattr(lab, "patch", lambda *args: pytest.fail("Unexpected write"))
    with pytest.raises(RuntimeError, match="cgroup"):
        lab.fault("oom")
    assert not (lab.directory / "fault.json").exists()


def test_pull_fault_really_enables_runtime_pull(lab, monkeypatch):
    patches = []
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "containment", lambda: {})
    monkeypatch.setattr(lab, "deployment", lambda name: deployment())
    monkeypatch.setattr(lab, "patch", lambda resource, changes: patches.extend(changes))
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    lab.fault("imagepull")
    assert {
        "op": "add",
        "path": "/spec/template/spec/containers/0/imagePullPolicy",
        "value": "Always",
    } in patches


def test_old_namespace_attachment_is_rejected(lab, monkeypatch):
    values = {"k3s": {"Id": "new"}, "prometheus": {"HostConfig": {"NetworkMode": "container:old"}}}
    monkeypatch.setattr(lab, "service", lambda service: values[service])
    with pytest.raises(RuntimeError, match="namespace"):
        lab.namespace_check()


def test_recreate_recreates_prometheus_after_k3s(lab, monkeypatch):
    calls = []
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "dispatch", lambda *args: None)
    monkeypatch.setattr(lab, "maintenance", lambda *args: {})
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(lab, "start_k3s", lambda recreate: calls.append(("start-k3s", recreate)))
    monkeypatch.setattr(lab, "namespace_check", lambda: {})
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    lab.recreate()
    node = next(index for index, args in enumerate(calls) if args == ("start-k3s", True))
    prom = next(
        index
        for index, args in enumerate(calls)
        if "--force-recreate" in args and args[-1] == "prometheus"
    )
    assert node < prom


@pytest.mark.parametrize(
    "running,exit_code,oom,address,expected",
    [
        (False, 1, False, "172.18.0.3", True),
        (True, 1, False, "172.18.0.3", False),
        (False, 137, True, "172.18.0.3", False),
        (False, 1, False, "172.18.0.4", False),
        (False, 2, False, "172.18.0.3", False),
    ],
)
def test_node_ip_retry_requires_exact_owned_transition(
    driver, running, exit_code, oom, address, expected
):
    node = {"State": {"Running": running, "ExitCode": exit_code, "OOMKilled": oom}}
    logs = '"NodeIPs changed for the node" node="cloudops-local" newNodeIPs=["172.18.0.3"] oldNodeIPs=["172.18.0.2"]'
    assert bool(driver.node_ip_change(node, logs, {address})) == expected


@pytest.mark.parametrize("replacement", [False, True])
def test_node_ip_startup_does_not_retry_twice_or_accept_replacement(
    lab, driver, monkeypatch, replacement
):
    from types import SimpleNamespace

    launched = {
        "Id": "same-container",
        "State": {"Running": True, "StartedAt": "2026-11-14T00:00:00Z"},
        "NetworkSettings": {"Networks": {"owned": {"IPAddress": "172.18.0.3"}}},
    }
    failed = {
        **launched,
        "State": {**launched["State"], "Running": False, "ExitCode": 1, "OOMKilled": False},
    }
    services = iter(
        [launched, failed, {**launched, "Id": "replacement"} if replacement else launched]
    )
    calls = []
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: None)
    monkeypatch.setattr(lab, "service", lambda name: next(services))
    monkeypatch.setattr(lab, "ownership", lambda: None)
    monkeypatch.setattr(lab, "command", lambda args: calls.append(args))

    def not_ready(*args):
        raise RuntimeError("not ready")

    monkeypatch.setattr(lab, "wait_k3s", not_ready)
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='"NodeIPs changed for the node" newNodeIPs=["172.18.0.3"] oldNodeIPs=["172.18.0.2"]',
            stderr="",
        ),
    )
    with pytest.raises(RuntimeError, match="replaced" if replacement else "not ready"):
        lab.start_k3s()
    assert calls == [["docker", "start", "same-container"]]


@pytest.mark.parametrize("recreate", [False, True])
def test_initial_node_can_acquire_missing_pin_but_recreate_reuses_artifact(
    lab, monkeypatch, recreate
):
    calls = []

    def dc(*args, **kwargs):
        calls.append(args)
        if not recreate and args[args.index("--pull") + 1] == "never":
            raise RuntimeError("Fresh host has no cached K3s image")

    monkeypatch.setattr(lab, "dc", dc)
    monkeypatch.setattr(
        lab,
        "service",
        lambda name: {
            "Id": "owned",
            "NetworkSettings": {"Networks": {"owned": {"IPAddress": "172.18.0.3"}}},
        },
    )
    monkeypatch.setattr(lab, "wait_k3s", lambda *args: None)
    monkeypatch.setattr(lab, "bound_node", lambda: None)
    lab.start_k3s(recreate=recreate)
    assert calls[0][calls[0].index("--pull") + 1] == ("never" if recreate else "missing")


def test_sample_histogram_measures_observations():
    sample = module(ROOT / "config/lab/sample_app.py", "sample_output_test")
    sample.observe(200, 0.02)
    sample.observe(503, 0.4)
    text = sample.metrics()
    assert 'lab_http_requests_total{status="200"} 1' in text
    assert 'lab_http_requests_total{status="503"} 1' in text
    assert 'lab_http_request_duration_seconds_bucket{le="0.01"} 0' in text
    assert 'lab_http_request_duration_seconds_bucket{le="0.05"} 1' in text
    assert 'lab_http_request_duration_seconds_bucket{le="+Inf"} 2' in text
    assert "lab_http_request_duration_seconds_count 2" in text
    assert sample.COUNTS != Counter()


@pytest.mark.parametrize(
    "target,seconds,rate",
    [("external", 1, 1), ("checkout", 21601, 1), ("history", 1, 31), ("checkout", 0, 1)],
)
def test_load_bounds_reject_before_any_network(target, seconds, rate, monkeypatch):
    load = module(ROOT / "config/lab/load.py", "bounded_load_test")
    monkeypatch.setattr(load, "build_opener", lambda *args: pytest.fail("Unexpected network"))
    with pytest.raises(ValueError):
        load.run(target, seconds, rate)


def test_monitoring_has_minimal_resources_and_valid_args():
    objects = list(yaml.safe_load_all((ROOT / "config/lab/monitoring.yaml").read_text()))
    roles = [value for value in objects if value["kind"] in {"Role", "ClusterRole"}]
    assert not any(
        resource in {"nodes/proxy", "secrets"}
        for role in roles
        for rule in role["rules"]
        for resource in rule["resources"]
    )
    deployment = next(value for value in objects if value["kind"] == "Deployment")
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--resources=pods,deployments,replicasets" in args


def test_rendered_compose_is_bounded_and_does_not_mount_truth_or_cluster_into_prometheus(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Compose rendering requires the host Docker CLI.")
    env = tmp_path / "lab.env"
    env.write_text(
        f"POSTGRES_PASSWORD=syntheticOnly\nCLOUDOPS_LAB_OWNER={uuid4()}\nCLOUDOPS_LAB_DIR={tmp_path}\nLAB_SAMPLE_IMAGE=cloudops-local-testing-lab-sample:synthetic\nLAB_SYSTEM_CPU=8500m\nLAB_SYSTEM_MEMORY=12000000000\n"
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env),
        "-p",
        "cloudops-local-testing-lab",
        "--profile",
        "lab-tools",
        "--profile",
        "lab-krr",
        "--profile",
        "observation",
        "--profile",
        "checks",
        "--profile",
        "maintenance",
    ]
    for file in (
        "compose.yaml",
        "compose.https.yaml",
        "compose.integration.yaml",
        "compose.alertmanager.yaml",
        "compose.lab.yaml",
    ):
        command += ["-f", file]
    result = subprocess.run(
        [*command, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    initializer = services["executor-init"]
    assert initializer["image"] == services["executor"]["image"]
    assert initializer["labels"]["io.cloudops.lab.owner"]
    assert config["volumes"]["executor_identity"]["labels"]["io.cloudops.lab.owner"]
    assert {mount["target"] for mount in initializer["volumes"]} == {
        "/var/lib/cloudops-executor",
        "/run/cloudops-executor-identity",
    }
    pin = next(
        mount
        for mount in services["executor"]["volumes"]
        if mount["target"] == "/run/cloudops-executor-identity"
    )
    assert pin["read_only"]
    steady = [
        services[name]
        for name in (
            "k3s",
            "prometheus",
            "db",
            "api",
            "worker",
            "observer",
            "executor",
            "https",
            "alertmanager",
            "krr",
            "load",
        )
    ]
    assert sum(int(value["mem_limit"]) for value in steady) <= 8.5 * 1024**3
    assert sum(float(value["cpus"]) for value in steady) <= 8
    assert all(value["mem_limit"] == value["memswap_limit"] for value in steady)
    assert services["prometheus"]["network_mode"] == "service:k3s"
    assert not services["prometheus"].get("privileged")
    assert all(
        "k3s_data" not in volume["source"] and "fault" not in volume["source"]
        for volume in services["prometheus"]["volumes"]
    )
    assert services["api"]["environment"]["DATABASE_URL"].endswith("/cloudops_lab")
    assert (
        services["test"]["environment"]["TEST_DATABASE_URL"].endswith("/cloudops_checks_test")
        if "test" in services
        else True
    )
    assert all(
        port["host_ip"] == "127.0.0.1"
        for value in services.values()
        for port in value.get("ports", [])
    )


@pytest.mark.parametrize(
    "memory,swap,cpu",
    [(0, 0, 4000000000), (4294967296, -1, 4000000000), (4294967296, 4294967296, 0)],
)
def test_unbounded_docker_parent_blocks_stress(lab, monkeypatch, memory, swap, cpu):
    monkeypatch.setattr(lab, "credentials_valid", lambda: None)
    monkeypatch.setattr(
        lab,
        "service",
        lambda name: {"HostConfig": {"Memory": memory, "MemorySwap": swap, "NanoCpus": cpu}},
    )
    with pytest.raises(RuntimeError, match="containment"):
        lab.containment()


@pytest.mark.parametrize("cpu,memory", [("12", "3Gi"), ("3", "14Gi")])
def test_oversold_node_allocatable_blocks_stress_without_patching_node(
    lab, monkeypatch, cpu, memory
):
    monkeypatch.setattr(lab, "credentials_valid", lambda: None)
    monkeypatch.setattr(
        lab,
        "service",
        lambda name: {
            "Id": "owned-parent",
            "HostConfig": {"Memory": 4294967296, "MemorySwap": 4294967296, "NanoCpus": 4000000000},
            "State": {"Pid": 999999},
        },
    )
    original = Path.read_text
    readings = {
        "/proc/999999/cgroup": "0::/owned-parent\n",
        "/sys/fs/cgroup/owned-parent/memory.max": "4294967296",
        "/sys/fs/cgroup/owned-parent/memory.swap.max": "0",
        "/sys/fs/cgroup/owned-parent/cpu.max": "400000 100000",
    }
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **k: readings[str(path)]
        if str(path) in readings
        else original(path, *a, **k),
    )
    calls = []
    node = {
        "spec": {},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "conditions": [
                {"type": key, "status": value}
                for key, value in {
                    "Ready": "True",
                    "MemoryPressure": "False",
                    "DiskPressure": "False",
                    "PIDPressure": "False",
                }.items()
            ],
        },
    }

    def kube(*args, **kwargs):
        calls.append(args)
        return json.dumps({"items": [node]})

    monkeypatch.setattr(lab, "kube", kube)
    with pytest.raises(RuntimeError, match="advertises unavailable"):
        lab.containment()
    assert calls == [("get", "nodes", "-o", "json")]


@pytest.mark.parametrize("layout", ["systemd", "cgroupfs"])
def test_bounded_container_parent_accepts_unlimited_init_subgroup(driver, tmp_path, layout):
    identity = "a" * 64
    parent = tmp_path / (f"docker-{identity}.scope" if layout == "systemd" else identity)
    subgroup = parent / "init"
    subgroup.mkdir(parents=True)
    for path in (parent, subgroup):
        for name, value in {
            "memory.max": str(4 * driver.GIB),
            "memory.swap.max": "0",
            "cpu.max": "400000 100000",
        }.items():
            (path / name).write_text(value if path == parent else "max")
    assert driver.container_cgroup(subgroup, identity) == parent


def test_wrong_container_ancestor_cannot_supply_containment(driver, tmp_path):
    with pytest.raises(RuntimeError, match="exact container"):
        driver.container_cgroup(tmp_path / ("b" * 64) / "init", "a" * 64)


def test_pod_process_outside_bounded_container_is_refused(driver, monkeypatch):
    outside = Path("/sys/fs/cgroup/another-container/pod")
    monkeypatch.setattr(driver, "process_cgroup", lambda pid: outside)
    with pytest.raises(RuntimeError, match="does not descend"):
        driver.pod_cgroup(123, outside, Path("/sys/fs/cgroup/owned-container"))


def test_unbounded_container_cannot_borrow_limits_from_host_ancestor(driver, tmp_path):
    identity = "a" * 64
    parent = tmp_path / identity
    parent.mkdir()
    for path, value in ((tmp_path, str(4 * driver.GIB)), (parent, "max")):
        (path / "memory.max").write_text(value)
        (path / "cpu.max").write_text("max 100000")
    with pytest.raises(RuntimeError, match="Effective"):
        driver.container_cgroup(parent / "init", identity)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026Tinvalid error\n",
        "2026-11-14T00:00:00 message without timezone\n",
        "unable to retrieve container logs\n",
    ],
)
def test_raw_log_comparator_does_not_invent_timestamps(driver, text):
    assert not driver.timestamped_nonempty(text)


def test_raw_log_comparator_accepts_rfc3339nano(driver):
    assert driver.timestamped_nonempty("2026-11-14T00:00:00.123456789Z startup progress\n")


def test_actual_failed_restarts_require_backoff_proof(driver):
    status = {"restartCount": 3, "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}
    assert driver.retry_failure(status, [{"reason": "BackOff"}])
    assert not driver.retry_failure(status, [{"reason": "Scheduled"}])
    assert not driver.retry_failure({**status, "restartCount": 0}, [{"reason": "BackOff"}])
    assert not driver.retry_failure(
        {"restartCount": 3, "state": {"terminated": {"exitCode": 0}}}, [{"reason": "BackOff"}]
    )


def test_init_fault_records_server_defaults_for_conflict_safe_reset(lab, monkeypatch):
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "containment", lambda: {})
    monkeypatch.setattr(lab, "deployment", lambda name: deployment())
    monkeypatch.setattr(lab, "patch", lambda *args: None)
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    lab.fault("init-retry")
    expected = json.loads((lab.directory / "fault.json").read_text())["expected"]["initContainers"][
        0
    ]
    assert expected["terminationMessagePath"] == "/dev/termination-log"
    assert expected["terminationMessagePolicy"] == "File"


def test_observe_refuses_same_label_pod_from_foreign_controller(lab, monkeypatch):
    (lab.directory / "fault.json").write_text(json.dumps({"uid": "original", "scenario": "oom"}))
    foreign = {
        "metadata": {
            "uid": "foreign-pod",
            "labels": {"app": "checkout", "cloudops-lab-workload-uid": "original"},
            "ownerReferences": [
                {"controller": True, "kind": "ReplicaSet", "name": "foreign", "uid": "foreign-rs"}
            ],
        },
        "status": {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]},
    }
    monkeypatch.setattr(lab, "deployment", lambda name: deployment())
    monkeypatch.setattr(lab, "kube", lambda *a: json.dumps({"items": [foreign]}))
    monkeypatch.setattr(
        lab,
        "get",
        lambda *a: {
            "metadata": {
                "uid": "foreign-rs",
                "ownerReferences": [
                    {"controller": True, "kind": "Deployment", "uid": "foreign-deployment"}
                ],
            }
        },
    )
    monkeypatch.setattr(lab, "wait", lambda observation, *a: observation())
    with pytest.raises(RuntimeError, match="real controller chain"):
        lab.observe()


def test_start_quarantines_existing_runtime_before_build(lab, driver, monkeypatch):
    lab.marker["sample_image"] = driver.SAMPLE_IMAGE
    calls = []
    monkeypatch.setattr(lab, "preflight", lambda: {})
    monkeypatch.setattr(lab, "initialize", lambda host: None)
    monkeypatch.setattr(lab, "images", lambda: {})
    monkeypatch.setattr(lab, "volume_guard", lambda **kwargs: None)
    monkeypatch.setattr(
        lab,
        "ownership",
        lambda: {
            "container": [
                {
                    "Config": {"Labels": {"com.docker.compose.service": "executor"}},
                    "State": {"Running": True},
                }
            ]
        },
    )
    monkeypatch.setattr(lab, "dispatch", lambda *args: calls.append(("dispatch", *args)))
    monkeypatch.setattr(lab, "maintenance", lambda *args: calls.append(("maintenance", *args)))

    def dc(*args, **kwargs):
        calls.append(args)
        if args[0] == "build":
            raise RuntimeError("test stopped at build boundary")
        return json.dumps({"permit": {"mode": "normal"}})

    monkeypatch.setattr(lab, "dc", dc)
    with pytest.raises(RuntimeError, match="build boundary"):
        lab.start()
    assert (
        calls.index(("dispatch", "enter-recovery"))
        < calls.index(("maintenance", "enter_recovery"))
        < calls.index(("build", "api"))
    )


def test_start_refuses_to_serve_when_authority_initialization_fails(lab, driver, monkeypatch):
    lab.marker["sample_image"] = driver.SAMPLE_IMAGE
    for name in ("preflight", "images"):
        monkeypatch.setattr(lab, name, lambda: {})
    monkeypatch.setattr(lab, "initialize", lambda host: None)
    monkeypatch.setattr(lab, "ownership", lambda: {"container": []})
    monkeypatch.setattr(lab, "volume_guard", lambda **kwargs: None)
    for name in ("start_k3s", "seed", "credentials_valid", "tls_config"):
        monkeypatch.setattr(lab, name, lambda: None)
    monkeypatch.setattr(lab, "credential", lambda kind: None)
    monkeypatch.setattr(lab, "kube", lambda *args, **kwargs: "")
    monkeypatch.setattr(lab, "command", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    calls = []

    def dc(*args, **kwargs):
        calls.append(args)
        if "executor-init" in args:
            raise RuntimeError("Pinned authority unavailable")
        return ""

    monkeypatch.setattr(lab, "dc", dc)
    with pytest.raises(RuntimeError, match="Pinned authority unavailable"):
        lab.start()
    assert ("run", "--rm", "-T", "--no-deps", "migrate") in calls
    assert not any(args[0] == "up" and set(driver.APP_SERVICES) & set(args) for args in calls)


@pytest.mark.parametrize("volumes", [[], [{"Name": "authority", "CreatedAt": "new-time"}]])
def test_missing_or_replaced_authority_volume_cannot_be_reinitialized(lab, monkeypatch, volumes):
    lab.marker["volumes"] = {"authority": "original-time"}
    monkeypatch.setattr(lab, "ownership", lambda: {"volume": volumes})
    with pytest.raises(RuntimeError, match="volume is missing or replaced"):
        lab.volume_guard(remember=True)
    assert lab.marker["volumes"] == {"authority": "original-time"}


def test_credential_probe_container_snippets_are_self_contained():
    import ast

    tree = ast.parse((ROOT / "scripts/probe-lab-credentials.py").read_text())
    snippets = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "Path(" in node.value
    ]
    assert len(snippets) == 2
    for snippet in snippets:
        compile(snippet, "container credential probe", "exec")
        assert "from pathlib import Path" in snippet
        assert "local_lab" not in snippet


def test_load_measures_both_fixed_destinations_without_real_network(monkeypatch):
    load = module(ROOT / "config/lab/load.py", "measured_load_test")
    urls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b"completed"

    class Opener:
        def open(self, url, timeout):
            urls.append(url)
            return Response()

    monkeypatch.setattr(load, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(load.time, "sleep", lambda seconds: None)
    result = load.run("both", 2, 2)
    assert result["responses"] == {"200": 4}
    assert sorted(urls) == ["http://k3s:30080/work"] * 2 + ["http://k3s:30081/work"] * 2
    assert result["started_at"] <= result["finished_at"]


def test_load_cli_accepts_both_but_refuses_invalid_duration_without_network():
    result = subprocess.run(
        [sys.executable, str(ROOT / "config/lab/load.py"), "--target", "both", "--seconds", "0"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "ValueError" in result.stderr and "invalid choice" not in result.stderr


def test_history_replacement_compares_one_exact_query_time_and_syncs_only_reader(lab, monkeypatch):
    from urllib.parse import parse_qs

    lab.marker["deployments"]["history"] = "original"
    queries, commands, credentials = [], [], []
    monkeypatch.setattr(lab, "no_execution", lambda: None)
    monkeypatch.setattr(lab, "deployment", lambda name: deployment())

    def prom(path):
        query = parse_qs(path.split("?", 1)[1])
        assert "time" in query
        queries.append(query)
        return {
            "data": {
                "result": [
                    {"metric": {"deployment_uid": "original"}, "value": [query["time"][0], "10"]}
                ]
            }
        }

    monkeypatch.setattr(lab, "prom", prom)
    monkeypatch.setattr(lab, "kube", lambda *args, **kwargs: "")
    monkeypatch.setattr(lab, "wait", lambda *args: None)
    monkeypatch.setattr(
        lab, "seed", lambda: lab.marker["deployments"].update(history="replacement")
    )
    monkeypatch.setattr(lab, "credential", lambda kind: credentials.append(kind))
    monkeypatch.setattr(lab, "dc", lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(lab, "receipt", lambda *args: None)
    lab.replace_history()
    assert queries[0] == queries[1]
    assert credentials == ["reader"]
    assert commands.index(("run", "--rm", "-T", "--no-deps", "migrate")) < commands.index(
        ("restart", "api", "worker", "observer")
    )
