#!/usr/bin/env python3
"""Operate the single owned lab; never selects host Kubernetes or restores application data."""

import argparse
import base64
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from uuid import UUID, uuid4

import yaml

from local_lab import OWNED_PROJECT, OWNED_PROJECTS, RECOVERY_LAB_PROJECT, ROOT, private_write
from release_scenarios import (
    load_catalog,
    read_json,
    sha256_file,
    validate_catalog,
    validate_qualification_receipt,
    validate_recovery_post_check,
    validate_trial_receipt,
)

DIRECTORY = ROOT / OWNED_PROJECTS[OWNED_PROJECT]
# The E10 S3 lab runs the qualified E07 recovery sample for its recovery scenarios.
RECOVERY_LAB = OWNED_PROJECT == RECOVERY_LAB_PROJECT
OWNER_LABEL = "io.cloudops.lab.owner"
GIB = 1024**3
SAMPLE_IMAGE = (
    f"{OWNED_PROJECT}-sample:"
    + hashlib.sha256(
        b"".join(
            (ROOT / file).read_bytes()
            for file in (
                "config/lab/Dockerfile.sample",
                "config/lab/sample_app.py",
                "config/lab/load.py",
                "config/lab/recovery_load.py",
            )
        )
    ).hexdigest()[:16]
)
SAMPLE_CTR_IMAGE = "docker.io/library/" + SAMPLE_IMAGE
RECOVERY_SOURCES = (
    "config/lab/Dockerfile.recovery",
    "config/lab/recovery_app.py",
    "config/lab/sample_app.py",
    "config/lab/load.py",
    "config/lab/recovery_load.py",
)
RECOVERY_SOURCE_VERSION = hashlib.sha256(
    b"".join((ROOT / file).read_bytes() for file in RECOVERY_SOURCES)
).hexdigest()
RECOVERY_IMAGES = {
    variant: f"{OWNED_PROJECT}-recovery:{RECOVERY_SOURCE_VERSION[:16]}-{variant}"
    for variant in ("healthy", "bad-image")
}
# E07-qualified healthy envelope for the 160 MiB resident recovery sample.
RECOVERY_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "192Mi"},
    "limits": {"cpu": "100m", "memory": "256Mi"},
}
# The local fault for each recovery scenario. The action repairs the fault itself;
# the fixed offered load is the treatment that must stay in place.
RECOVERY_FAULTS = {
    "recovery-oom-memory": "memory-limit",
    "recovery-bad-rollout-restore": "bad-rollout",
    "recovery-traffic-scale": "overload",
}
RECOVERY_POLICY_ID = "e10-s3-request-health/v1"
FAULT_MEMORY_LIMIT = "128Mi"
# Per-container containment bound; the recovery sample's qualified limit is 256 MiB.
SAMPLE_MEMORY_ENVELOPE = (512 if RECOVERY_LAB else 160) * 1024**2
APP_SERVICES = ("api", "worker", "observer", "executor")
FILES = (
    "compose.yaml",
    "compose.https.yaml",
    "compose.integration.yaml",
    "compose.alertmanager.yaml",
    "compose.lab.yaml",
    *(("compose.lab-recovery.yaml",) if RECOVERY_LAB else ()),
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def timestamped_nonempty(text):
    if not text:
        return False
    for line in text.splitlines():
        if not line.split():
            return False
        try:
            stamp = datetime.fromisoformat(line.split()[0].replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            return False
    return True


def process_cgroup(pid):
    group = next(
        line.split(":", 2)[2]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def container_cgroup(subgroup, container_id):
    parent = next(
        (
            path
            for path in (subgroup, *subgroup.parents)
            if path.name in {container_id, f"docker-{container_id}.scope"}
        ),
        None,
    )
    require(
        parent is not None,
        "K3s PID has no exact container cgroup ancestor; stress blocked.",
    )
    memory = (parent / "memory.max").read_text().strip()
    quota, period = (parent / "cpu.max").read_text().split()
    require(
        memory == str(4 * GIB)
        and (parent / "memory.swap.max").read_text().strip() == "0"
        and quota != "max"
        and int(quota) > 0
        and int(period) > 0
        and int(quota) / int(period) == 4,
        "Effective K3s cgroup limits are missing.",
    )
    return parent


def pod_cgroup(pid, observed, parent):
    actual = process_cgroup(pid)
    require(
        actual == observed and actual != parent and actual.is_relative_to(parent),
        "Pod process does not descend from the limited K3s cgroup; stress blocked.",
    )
    return actual


def retry_failure(status, events):
    terminated = status.get("state", {}).get("terminated") or status.get("lastState", {}).get(
        "terminated", {}
    )
    return status.get("restartCount", 0) > 0 and (
        status.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
        or (
            terminated.get("exitCode", 0) != 0
            and any(event.get("reason") == "BackOff" for event in events)
        )
    )


def node_ip_change(node, logs, addresses):
    state = node["State"]
    if state["Running"] or state["ExitCode"] != 1 or state.get("OOMKilled"):
        return None
    for line in reversed(logs.splitlines()):
        if '"NodeIPs changed for the node"' not in line:
            continue
        old = re.search(r"oldNodeIPs=(\[[^\]]*\])", line)
        new = re.search(r"newNodeIPs=(\[[^\]]*\])", line)
        if old and new:
            try:
                old, new = json.loads(old[1]), json.loads(new[1])
            except ValueError:
                continue
            if new and old != new and set(new) <= addresses:
                return {"old": old, "new": new}
    return None


def conditional_patch(resource, uid, changes):
    require(resource["metadata"]["uid"] == uid, "UID changed; no mutation sent.")
    return [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": resource["metadata"]["resourceVersion"],
        },
        *changes,
    ]


def owned_fields(deployment):
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    return {
        "image": container["image"],
        "resources": container["resources"],
        "imagePullPolicy": container.get("imagePullPolicy", "IfNotPresent"),
        "mode": next(
            (entry for entry in container.get("env", []) if entry["name"] == "LAB_MODE"),
            None,
        ),
        "initContainers": spec.get("initContainers"),
    }


def field_changes(deployment, wanted):
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    env = [entry for entry in container.get("env", []) if entry["name"] != "LAB_MODE"]
    if wanted["mode"] is not None:
        env.append(wanted["mode"])
    changes = [
        {
            "op": "add",
            "path": "/spec/template/spec/containers/0/imagePullPolicy",
            "value": wanted["imagePullPolicy"],
        },
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/0/image",
            "value": wanted["image"],
        },
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/0/resources",
            "value": wanted["resources"],
        },
        {"op": "add", "path": "/spec/template/spec/containers/0/env", "value": env},
    ]
    if wanted["initContainers"] is not None:
        changes.append(
            {
                "op": "add",
                "path": "/spec/template/spec/initContainers",
                "value": wanted["initContainers"],
            }
        )
    elif "initContainers" in spec:
        changes.append({"op": "remove", "path": "/spec/template/spec/initContainers"})
    return changes


def namespace_recreate_arguments():
    return ["up", "-d", "--no-deps", "--force-recreate", "prometheus"]


class Lab:
    def __init__(self, directory=DIRECTORY):
        self.directory = Path(directory)
        require(
            self.directory.absolute() == DIRECTORY and self.directory.resolve() == DIRECTORY,
            "Use the fixed project-specific directory without symlinks.",
        )
        self.marker_path = self.directory / "owner.json"
        self.trial_path = self.directory / "trial.json"
        self.marker = None
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "POSTGRES_PASSWORD",
                "LAB_SAMPLE_IMAGE",
                "DOCKER_CONTEXT",
                "BUILDX_CONFIG",
                "BUILDX_BUILDER",
                "BUILDKIT_HOST",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
                "DOCKER_TLS",
                "KUBECONFIG",
                "EXECUTOR_PROOF_PROVIDER",
            }
            and not key.startswith(("CLOUDOPS_LAB_", "LAB_SYSTEM_"))
        }
        self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        self.env["DOCKER_CONFIG"] = str(self.directory / "docker-config")
        self.env["CLOUDOPS_LAB_PROJECT"] = OWNED_PROJECT
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(self.directory / "lab.env"),
            "-p",
            OWNED_PROJECT,
            "--profile",
            "observation",
        ]
        for file in FILES:
            self.compose += ["-f", file]

    def command(self, argv, body=None, timeout=60, stdout=None):
        result = subprocess.run(
            argv,
            input=body,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=self.env,
            timeout=timeout,
            text=not isinstance(body, bytes) and stdout is None,
        )
        require(
            result.returncode == 0,
            f"{argv[0]} operation failed (exit {result.returncode}); no success claimed.",
        )
        return result.stdout

    def dc(self, *args, **kwargs):
        return self.command([*self.compose, *args], **kwargs)

    def kube(self, *args, body=None, timeout=60):
        return self.dc(
            "exec",
            "-T",
            "k3s",
            "/bin/kubectl",
            *args,
            body=json.dumps(body) if isinstance(body, (dict, list)) else body,
            timeout=timeout,
        )

    def get(self, kind, name, namespace=None):
        args = ["get", kind, name, "--ignore-not-found", "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        value = self.kube(*args)
        return json.loads(value) if value.strip() else None

    def save(self):
        private_write(self.marker_path, json.dumps(self.marker, indent=2) + "\n")

    def volume_guard(self, remember=False):
        volumes = {item["Name"]: item["CreatedAt"] for item in self.ownership()["volume"]}
        require(
            all(
                volumes.get(name) == created
                for name, created in self.marker.get("volumes", {}).items()
            ),
            "A recorded data/authority volume is missing or replaced; automatic recreation refused.",
        )
        if remember:
            self.marker["volumes"] = volumes
            self.save()

    def resources(self):
        result = {}
        for kind, listing in (
            ("container", ["ps", "-aq"]),
            ("volume", ["volume", "ls", "-q"]),
            ("network", ["network", "ls", "-q"]),
        ):
            ids = self.command(
                [
                    "docker",
                    *listing,
                    "--filter",
                    f"label=com.docker.compose.project={OWNED_PROJECT}",
                ]
            ).split()
            if kind in {"volume", "network"}:
                named = self.command(
                    ["docker", *listing, "--filter", f"name=^{OWNED_PROJECT}_"]
                ).split()
                ids = list(dict.fromkeys([*ids, *named]))
            result[kind] = (
                json.loads(self.command(["docker", kind, "inspect", *ids])) if ids else []
            )
        return result

    def ownership(self, allow_new=False):
        inventory = self.resources()
        if not self.marker_path.exists():
            require(
                allow_new and not any(inventory.values()),
                "No ownership marker; refusing to adopt existing project resources.",
            )
            return inventory
        require(
            self.marker_path.is_file()
            and not self.marker_path.is_symlink()
            and not self.marker_path.stat().st_mode & 0o077,
            "Ownership marker must be a private regular file.",
        )
        marker = json.loads(self.marker_path.read_text())
        require(
            marker.get("project") == OWNED_PROJECT
            and marker.get("root") == str(ROOT)
            and str(UUID(marker["owner"])) == marker["owner"],
            "Ownership marker does not match this checkout.",
        )
        for kind, values in inventory.items():
            for item in values:
                labels = (
                    item.get("Config", {}).get("Labels", {})
                    if kind == "container"
                    else item.get("Labels", {})
                )
                require(
                    labels.get(OWNER_LABEL) == marker["owner"]
                    and labels.get("com.docker.compose.project") == OWNED_PROJECT,
                    "Project resource ownership mismatch; no mutation allowed.",
                )
                if kind == "container":
                    if labels.get("com.docker.compose.service") == "k3s":
                        expected = {
                            "/var/lib/rancher/k3s": "k3s_data",
                            "/var/log": "k3s_logs",
                            "/var/lib/cni": "k3s_cni",
                            "/var/lib/kubelet": "k3s_kubelet",
                        }
                        actual = {
                            mount["Destination"]: mount.get("Name")
                            for mount in item.get("Mounts", [])
                            if mount["Type"] == "volume"
                        }
                        require(
                            actual
                            == {path: OWNED_PROJECT + "_" + name for path, name in expected.items()}
                            and not any(
                                mount["Type"] == "bind" for mount in item.get("Mounts", [])
                            ),
                            "Unexpected K3s mount; refuse anonymous or foreign storage before mutation.",
                        )
                    require(
                        labels.get("com.docker.compose.project.working_dir") == str(ROOT),
                        "Container belongs to another checkout.",
                    )
                    require(
                        all(
                            binding["HostIp"] == "127.0.0.1"
                            for bindings in item["HostConfig"].get("PortBindings", {}).values()
                            for binding in bindings or []
                        ),
                        "An owned endpoint is published beyond loopback.",
                    )
        self.marker = marker
        if (self.directory / "lab.env").exists():
            require(
                not (self.directory / "lab.env").is_symlink()
                and not (self.directory / "lab.env").stat().st_mode & 0o077,
                "Generated environment must remain private.",
            )
        return inventory

    def receipt(self, operation, value):
        source = self.command(["git", "rev-parse", "HEAD"]).strip()
        paths = [ROOT / file for file in FILES]
        paths += list((ROOT / "config/lab").glob("*"))
        paths += [Path(__file__), ROOT / "scripts/local_lab.py"]
        paths += [ROOT / "scripts/release_scenarios.py"]
        paths += list((ROOT / "services/backend/tests/fixtures/e10_s3").glob("*.json"))
        hashes = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        }
        value = {
            "operation": operation,
            "at": datetime.now(timezone.utc).isoformat(),
            "project": OWNED_PROJECT,
            "owner": self.marker["owner"],
            "source_commit": source,
            "source_files": hashes,
            "working_diff_sha256": hashlib.sha256(
                self.command(["git", "diff", "--binary"]).encode()
            ).hexdigest(),
            "deployment_uids": self.marker["deployments"],
            **value,
        }
        path = self.directory / "receipts" / f"{time.time_ns()}-{operation}.json"
        private_write(path, json.dumps(value, indent=2) + "\n")
        print(json.dumps(value, indent=2))

    def preflight(self):
        inventory = self.ownership(allow_new=True)
        for port, service in ((6445, "k3s"), (8443, "https")):
            if not any(
                item["State"]["Running"]
                and item["Config"]["Labels"].get("com.docker.compose.service") == service
                for item in inventory["container"]
            ):
                with socket.socket() as check:
                    check.bind(("127.0.0.1", port))
        info = json.loads(self.command(["docker", "info", "--format", "{{json .}}"]))
        require(
            int(info["ServerVersion"].split(".")[0]) >= 28
            and info["OSType"] == "linux"
            and info["Architecture"] in {"x86_64", "amd64"}
            and info["CgroupVersion"] == "2"
            and info["MemoryLimit"]
            and info["SwapLimit"],
            "Need Linux/amd64 Docker 28+, cgroup v2 and memory/swap limits.",
        )
        require(
            info["NCPU"] >= 8 and info["MemTotal"] >= 10 * GIB,
            "Insufficient host capacity.",
        )
        require(shutil.disk_usage(ROOT).free >= 20 * GIB, "Need at least 20 GiB free disk.")
        routes = json.loads(self.command(["ip", "-j", "route", "show", "table", "all"]))
        cidrs = [ipaddress.ip_network(cidr) for cidr in ("10.244.0.0/16", "10.245.0.0/16")]
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default":
                continue
            network = ipaddress.ip_network(destination, strict=False)
            require(
                not any(
                    network.version == cidr.version and network.overlaps(cidr) for cidr in cidrs
                ),
                "Lab CIDR overlaps a host route.",
            )
        networks = self.command(["docker", "network", "ls", "-q"]).split()
        for network in (
            json.loads(self.command(["docker", "network", "inspect", *networks]))
            if networks
            else []
        ):
            for entry in (network.get("IPAM") or {}).get("Config") or []:
                if "Subnet" in entry:
                    subnet = ipaddress.ip_network(entry["Subnet"])
                    require(
                        not any(
                            subnet.version == cidr.version and subnet.overlaps(cidr)
                            for cidr in cidrs
                        ),
                        "Lab CIDR overlaps Docker network.",
                    )
        return {
            "docker": info["ServerVersion"],
            "cpus": info["NCPU"],
            "memory_bytes": info["MemTotal"],
        }

    def initialize(self, host):
        if self.marker is not None:
            require(
                (self.directory / "lab.env").is_file(),
                "Owned state has no environment; recover it explicitly.",
            )
            return
        require(
            not self.directory.exists(),
            "Unmarked generated directory exists; refusing adoption.",
        )
        self.directory.mkdir(mode=0o700, parents=True)
        self.marker = {
            "project": OWNED_PROJECT,
            "owner": str(uuid4()),
            "root": str(ROOT),
            "deployments": {},
            "sample_image": SAMPLE_IMAGE,
        }
        if RECOVERY_LAB:
            self.marker["recovery_images"] = RECOVERY_IMAGES
        self.save()
        values = {
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "CLOUDOPS_LAB_OWNER": self.marker["owner"],
            "CLOUDOPS_LAB_PROJECT": OWNED_PROJECT,
            "CLOUDOPS_LAB_DIR": str(self.directory),
            "LAB_SAMPLE_IMAGE": SAMPLE_IMAGE,
            "LAB_SYSTEM_CPU": f"{host['cpus'] * 1000 - 3500}m",
            "LAB_SYSTEM_MEMORY": str(host["memory_bytes"] - 4 * GIB + 256 * 1024**2),
        }
        private_write(
            self.directory / "lab.env",
            "".join(f"{key}={value}\n" for key, value in values.items()),
        )
        for name in (
            "reader",
            "writer",
            "dispatcher",
            "krr",
            "recommendations",
            "intake-api",
            "intake-sender",
            "prometheus",
            "monitoring",
            "receipts",
            *(("recovery",) if RECOVERY_LAB else ()),
        ):
            (self.directory / name).mkdir(mode=0o700)
        private_write(self.directory / "krr/key", secrets.token_urlsafe(32))
        private_write(self.directory / "intake-sender/token", secrets.token_urlsafe(32))
        private_write(
            self.directory / "intake-api/config.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "source_id": "lab-alertmanager",
                    "cluster": "local-k3s",
                    "token_sha256": hashlib.sha256(
                        (self.directory / "intake-sender/token").read_bytes()
                    ).hexdigest(),
                }
            ),
        )

    def images(self):
        folder = self.directory / "docker-config"
        require(not folder.is_symlink(), "Docker configuration must not be a symlink.")
        folder.mkdir(mode=0o700, exist_ok=True)
        require(
            not folder.stat().st_mode & 0o077,
            "Docker configuration directory must be private.",
        )
        config_path = folder / "config.json"
        if config_path.exists() or config_path.is_symlink():
            require(
                config_path.is_file()
                and not config_path.is_symlink()
                and not config_path.stat().st_mode & 0o077
                and json.loads(config_path.read_text()) == {},
                "Lab Docker configuration must contain only an empty public-registry configuration.",
            )
        else:
            private_write(config_path, "{}\n")
        config = json.loads(self.dc("config", "--format", "json"))
        refs = {
            service["image"]
            for service in config["services"].values()
            if "image" in service and not service["image"].startswith("cloudops-")
        }
        for path in (ROOT / "config/lab/monitoring.yaml",):
            for resource in yaml.safe_load_all(path.read_text()):
                refs.update(
                    container["image"]
                    for container in resource.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                )
        for file in (
            "services/backend/Dockerfile",
            "apps/web/Dockerfile.https",
            "services/krr/Dockerfile",
            "config/lab/Dockerfile.sample",
            *(("config/lab/Dockerfile.recovery",) if RECOVERY_LAB else ()),
        ):
            for line in (ROOT / file).read_text().splitlines():
                if line.startswith("FROM "):
                    refs.add(line.split()[1])
        verified = {}
        for ref in sorted(refs):
            receipt = (
                self.directory
                / "receipts"
                / ("registry-" + hashlib.sha256(ref.encode()).hexdigest() + ".json")
            )
            if "@" in ref and receipt.is_file() and not receipt.is_symlink():
                prior = json.loads(receipt.read_text())
                if prior.get("reference") == ref and prior.get("digest") == ref.rsplit("@", 1)[1]:
                    try:
                        image = json.loads(self.command(["docker", "image", "inspect", ref]))[0]
                    except RuntimeError:
                        image = {}
                    if (
                        image.get("Os") == "linux"
                        and image.get("Architecture") == "amd64"
                        and any(
                            value.endswith("@" + prior["digest"])
                            for value in image.get("RepoDigests", [])
                        )
                    ):
                        verified[ref] = prior["digest"]
                        continue
            manifest = json.loads(
                self.command(
                    [
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        "--format",
                        "{{json .Manifest}}",
                        ref,
                    ],
                    timeout=60,
                )
            )
            digest = manifest.get("digest")
            require(
                digest and ("@" not in ref or digest == ref.rsplit("@", 1)[1]),
                "Registry digest verification failed; no mutable-tag fallback allowed.",
            )
            verified[ref] = digest
            private_write(
                receipt,
                json.dumps(
                    {
                        "reference": ref,
                        "digest": digest,
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
        return {"remote_references_verified": verified}

    def ensure(self, resource):
        resource = copy.deepcopy(resource)
        metadata = resource["metadata"]
        metadata.setdefault("labels", {})[OWNER_LABEL] = self.marker["owner"]
        current = self.get(resource["kind"], metadata["name"], metadata.get("namespace"))
        if current is not None:
            require(
                current["metadata"].get("labels", {}).get(OWNER_LABEL) == self.marker["owner"],
                "Kubernetes object ownership mismatch.",
            )
            return current
        return json.loads(self.kube("create", "-f", "-", "-o", "json", body=resource))

    def deployment(self, name):
        resource = self.get("deployment", name, "demo")
        require(
            resource is not None
            and resource["metadata"].get("labels", {}).get(OWNER_LABEL) == self.marker["owner"],
            "Deployment ownership mismatch.",
        )
        require(
            self.marker["deployments"].get(name) == resource["metadata"]["uid"],
            "Deployment UID changed; synchronize explicitly, never rebind writer authority.",
        )
        return resource

    def pod_binding(self, pod):
        name = pod["metadata"].get("labels", {}).get("app")
        require(name in self.marker["deployments"], "Unexpected sample Pod.")
        self.deployment(name)
        owners = [
            owner for owner in pod["metadata"].get("ownerReferences", []) if owner.get("controller")
        ]
        require(
            len(owners) == 1 and owners[0]["kind"] == "ReplicaSet",
            "Unproven Pod controller.",
        )
        rs = self.get("replicaset", owners[0]["name"], "demo")
        require(
            rs is not None and rs["metadata"]["uid"] == owners[0]["uid"],
            "ReplicaSet identity changed.",
        )
        controllers = [
            owner for owner in rs["metadata"].get("ownerReferences", []) if owner.get("controller")
        ]
        require(
            len(controllers) == 1
            and controllers[0]["kind"] == "Deployment"
            and controllers[0]["uid"] == self.marker["deployments"][name]
            and pod["metadata"]["labels"].get("cloudops-lab-workload-uid") == controllers[0]["uid"],
            "Pod UID label does not match its real controller chain.",
        )

    def patch(self, resource, changes):
        metadata = resource["metadata"]
        self.kube(
            "patch",
            "deployment",
            metadata["name"],
            "-n",
            "demo",
            "--type=json",
            "-p",
            json.dumps(
                conditional_patch(resource, self.marker["deployments"][metadata["name"]], changes)
            ),
        )

    def seed(self):
        if "namespace_uid" in self.marker:
            current_namespace = self.get("namespace", "demo")
            require(
                current_namespace is not None
                and current_namespace["metadata"]["uid"] == self.marker["namespace_uid"],
                "Namespace identity changed; refusing recreation.",
            )
        resources = list(yaml.safe_load_all((ROOT / "config/lab/workload.yaml").read_text()))
        for resource in resources:
            if resource["kind"] != "Deployment":
                self.ensure(resource)
        for resource in yaml.safe_load_all((ROOT / "config/lab/monitoring.yaml").read_text()):
            self.ensure(resource)
        namespace = self.get("namespace", "demo")
        if "namespace_uid" in self.marker:
            require(
                namespace["metadata"]["uid"] == self.marker["namespace_uid"],
                "Namespace UID changed.",
            )
        self.marker["namespace_uid"] = namespace["metadata"]["uid"]
        for name in ("checkout", "history"):
            template = copy.deepcopy(
                next(resource for resource in resources if resource["kind"] == "Deployment")
            )
            template["metadata"]["name"] = name
            template["metadata"]["labels"]["app"] = name
            template["spec"]["selector"]["matchLabels"]["app"] = name
            template["spec"]["template"]["metadata"]["labels"]["app"] = name
            spec = template["spec"]["template"]["spec"]
            spec["automountServiceAccountToken"] = False
            container = spec["containers"][0]
            container.update(
                name="app",
                image=SAMPLE_CTR_IMAGE,
                imagePullPolicy="Never",
                command=["python", "/app/sample_app.py"],
                ports=[{"name": "metrics", "containerPort": 8080}],
                resources={
                    "requests": {"cpu": "50m", "memory": "32Mi"},
                    "limits": {"cpu": "400m", "memory": "160Mi"},
                },
            )
            container["readinessProbe"]["httpGet"]["path"] = "/health"
            if RECOVERY_LAB and name == "checkout":
                # The E07-qualified recovery sample: bounded resident memory, two serving
                # slots and an image variant that rejects startup. Recreate keeps one Pod so
                # a fault cannot hide behind an old ready Pod during a template rollout.
                container.update(
                    image="docker.io/library/" + RECOVERY_IMAGES["healthy"],
                    command=["python", "/app/recovery_app.py"],
                    resources=copy.deepcopy(RECOVERY_RESOURCES),
                )
                template["spec"]["strategy"] = {"type": "Recreate"}
            live = (
                self.deployment(name)
                if name in self.marker["deployments"]
                else self.ensure(template)
            )
            uid = live["metadata"]["uid"]
            if name in self.marker["deployments"]:
                require(
                    uid == self.marker["deployments"][name],
                    "Deployment replacement requires explicit scope reconciliation.",
                )
            else:
                self.marker["deployments"][name] = uid
                self.save()
            live = self.deployment(name)
            labels = live["spec"]["template"]["metadata"]["labels"]
            if labels.get("cloudops-lab-workload-uid") != uid:
                require(
                    "cloudops-lab-workload-uid" not in labels,
                    "Conflicting telemetry UID binding.",
                )
                self.patch(
                    live,
                    [
                        {
                            "op": "add",
                            "path": "/spec/template/metadata/labels/cloudops-lab-workload-uid",
                            "value": uid,
                        }
                    ],
                )
            self.ensure(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": name, "namespace": "demo"},
                    "spec": {
                        "type": "NodePort",
                        "selector": {"app": name},
                        "ports": [
                            {
                                "port": 8080,
                                "targetPort": 8080,
                                "nodePort": 30080 if name == "checkout" else 30081,
                            }
                        ],
                    },
                }
            )
        self.save()

    def reader_scope(self, config):
        from cloudops.features.workloads.scope_config import (
            LiveScope,
            cluster_fingerprint,
        )

        fingerprint = cluster_fingerprint(config)
        pinned = self.marker.get("cluster_fingerprint")
        prior_scope = self.directory / "reader/scope.json"
        if prior_scope.exists():
            previous_pin = LiveScope.model_validate(
                json.loads(prior_scope.read_text())
            ).cluster.expected_fingerprint
            require(
                pinned in {None, previous_pin},
                "Stored scope fingerprint differs from its ownership record.",
            )
            pinned = previous_pin
        previous = self.directory / "reader/client.json"
        if pinned is None and previous.exists():
            pinned = cluster_fingerprint(json.loads(previous.read_text()))
        require(
            pinned in {None, fingerprint},
            "Cluster fingerprint changed; token renewal cannot repin cluster identity.",
        )
        scope = LiveScope(
            schema_version=2,
            cluster={
                "key": "local-k3s",
                "fingerprint_algorithm": "server-ca-sha256/v1",
                "expected_fingerprint": fingerprint,
            },
            deployments=[
                {"namespace": "demo", "name": name} for name in sorted(self.marker["deployments"])
            ],
        )
        self.marker["cluster_fingerprint"] = fingerprint
        self.save()
        return scope.model_dump(mode="json")

    def credential(self, kind):
        ca_config = json.loads(self.kube("config", "view", "--raw", "-o", "json"))
        ca = ca_config["clusters"][0]["cluster"]["certificate-authority-data"]
        account = "cloudops-monitor" if kind == "monitoring" else f"cloudops-{kind}"
        namespace = "cloudops-lab-monitoring" if kind == "monitoring" else "demo"
        value = json.loads(
            self.kube(
                "create",
                "--raw",
                f"/api/v1/namespaces/{namespace}/serviceaccounts/{account}/token",
                "-f",
                "-",
                body={
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenRequest",
                    "spec": {"audiences": [], "expirationSeconds": 28800},
                },
            )
        )
        token, expiry = value["status"]["token"], value["status"]["expirationTimestamp"]
        folder = self.directory / kind
        if kind == "monitoring":
            private_write(folder / "token", token)
            private_write(folder / "ca.crt", base64.b64decode(ca).decode())
        else:
            client_config = {
                "server": "https://k3s:6443",
                "ca_data": ca,
                "token": token,
            }
            if kind == "reader":
                for name in self.marker["deployments"]:
                    self.deployment(name)
                private_write(folder / "scope.json", json.dumps(self.reader_scope(client_config)))
            workloads = [
                {
                    "cluster": "local-k3s",
                    "namespace": "demo",
                    "name": name,
                    "uid": uid,
                    "source": "live_kubernetes",
                }
                for name, uid in self.marker["deployments"].items()
                if kind == "reader" or name == "checkout"
            ]
            private_write(
                folder / "client.json",
                json.dumps(client_config),
            )
            private_write(folder / "workloads.json", json.dumps(workloads))
            private_write(
                folder / "kubeconfig.json",
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Config",
                        "clusters": [
                            {
                                "name": "lab",
                                "cluster": {
                                    "server": "https://k3s:6443",
                                    "certificate-authority-data": ca,
                                },
                            }
                        ],
                        "users": [{"name": kind, "user": {"token": token}}],
                        "contexts": [
                            {
                                "name": "lab",
                                "context": {
                                    "cluster": "lab",
                                    "user": kind,
                                    "namespace": "demo",
                                },
                            }
                        ],
                        "current-context": "lab",
                    }
                ),
            )
        private_write(folder / "expiry.json", json.dumps({"expires_at": expiry}))

    def credentials_valid(self, *, read_only=False):
        for kind in ("reader", "monitoring") if read_only else ("reader", "writer", "monitoring"):
            expiry = json.loads((self.directory / kind / "expiry.json").read_text())["expires_at"]
            require(
                datetime.fromisoformat(expiry.replace("Z", "+00:00")) > datetime.now(timezone.utc),
                f"{kind} credential expired; renew explicitly.",
            )
        for kind in ("reader",) if read_only else ("reader", "writer"):
            scope = json.loads((self.directory / kind / "workloads.json").read_text())
            expected = {
                name: uid
                for name, uid in self.marker["deployments"].items()
                if kind == "reader" or name == "checkout"
            }
            require(
                {(row["namespace"], row["name"]): row["uid"] for row in scope}
                == {("demo", name): uid for name, uid in expected.items()}
                and all(
                    row["cluster"] == "local-k3s" and row["source"] == "live_kubernetes"
                    for row in scope
                ),
                "Credential scope differs from recorded UIDs.",
            )
        from cloudops.features.workloads.scope_config import (
            LiveScope,
            cluster_fingerprint,
        )

        scope = LiveScope.model_validate(
            json.loads((self.directory / "reader/scope.json").read_text())
        )
        fingerprint = cluster_fingerprint(
            json.loads((self.directory / "reader/client.json").read_text())
        )
        require(
            scope.cluster.key == "local-k3s"
            and scope.cluster.expected_fingerprint
            == fingerprint
            == self.marker.get("cluster_fingerprint")
            and {(target.namespace, target.name) for target in scope.deployments}
            == {("demo", name) for name in self.marker["deployments"]},
            "Pinned application scope differs from recorded cluster or target names.",
        )

    def tls_config(self):
        self.dc("up", "-d", "--no-deps", "https")
        self.wait(
            lambda: self.dc(
                "exec",
                "-T",
                "https",
                "test",
                "-s",
                "/data/caddy/pki/authorities/local/root.crt",
            )
            == "",
            "HTTPS CA",
            60,
        )
        self.dc(
            "cp",
            "https:/data/caddy/pki/authorities/local/root.crt",
            str(self.directory / "prometheus/ca.crt"),
        )
        os.chmod(self.directory / "prometheus/ca.crt", 0o600)
        spec = importlib.util.spec_from_file_location(
            "lab_metric_queries", ROOT / "scripts/bootstrap-metrics.py"
        )
        metrics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metrics)
        config = {
            "schema": "PrometheusReadConfig/v1",
            "endpoint_id": "lab-prometheus",
            "producer_version": "prometheus/v3.13.5",
            "server": "https://https:8443/prometheus",
            "ca_data": base64.b64encode(
                (self.directory / "prometheus/ca.crt").read_bytes()
            ).decode(),
            "step_seconds": 30,
            "binding": {
                "deployment_uid_label": "deployment_uid",
                "namespace_label": "namespace",
            },
            "queries": metrics.QUERIES,
        }
        private_write(self.directory / "prometheus/read.json", json.dumps(config))
        private_write(
            self.directory / "intake-sender/proxy-ca.crt",
            (self.directory / "prometheus/ca.crt").read_text(),
        )

    def wait(self, observation, description, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if observation():
                    return
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for {description}; no success claimed.")

    def wait_k3s(self, launched, addresses):
        deadline, stable = time.monotonic() + 240, None
        while time.monotonic() < deadline:
            current = self.service("k3s")
            require(
                current["Id"] == launched["Id"]
                and current["State"]["StartedAt"] == launched["State"]["StartedAt"],
                "K3s identity changed during startup.",
            )
            require(current["State"]["Running"], "K3s exited during startup.")
            ready = False
            if current["State"].get("Health", {}).get("Status") == "healthy":
                try:
                    nodes = json.loads(self.kube("get", "nodes", "-o", "json"))["items"]
                    ready = len(nodes) == 1 and addresses <= {
                        value["address"]
                        for value in nodes[0]["status"]["addresses"]
                        if value["type"] == "InternalIP"
                    }
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            stable = (stable if stable is not None else time.monotonic()) if ready else None
            if stable is not None and time.monotonic() - stable >= 5:
                return
            time.sleep(1)
        raise RuntimeError("K3s readiness and Node IP did not converge within the startup bound.")

    def start_k3s(self, recreate=False, *, pull_missing=True):
        args = [
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "missing" if pull_missing and not recreate else "never",
        ]
        if recreate:
            args.append("--force-recreate")
        self.dc(*args, "k3s", timeout=60)
        launched = self.service("k3s")
        addresses = {
            network.get(key)
            for network in launched["NetworkSettings"]["Networks"].values()
            for key in ("IPAddress", "GlobalIPv6Address")
        } - {None, ""}
        require(addresses, "Cannot capture the launched K3s network identity.")
        try:
            self.wait_k3s(launched, addresses)
        except (RuntimeError, subprocess.TimeoutExpired):
            self.ownership()
            node = self.service("k3s")
            require(
                node["Id"] == launched["Id"]
                and node["State"]["StartedAt"] == launched["State"]["StartedAt"],
                "K3s changed before retry classification.",
            )
            result = subprocess.run(
                [
                    "docker",
                    "logs",
                    "--since",
                    node["State"]["StartedAt"],
                    "--tail",
                    "2000",
                    node["Id"],
                ],
                capture_output=True,
                text=True,
                env=self.env,
                cwd=ROOT,
                timeout=30,
            )
            change = (
                node_ip_change(node, result.stdout + result.stderr, addresses)
                if result.returncode == 0
                else None
            )
            require(
                change is not None,
                "K3s startup failed without a verified Node IP transition; automatic retry refused.",
            )
            self.command(["docker", "start", node["Id"]])
            restarted = self.service("k3s")
            require(
                restarted["Id"] == node["Id"],
                "K3s container was replaced during its single restart.",
            )
            current_addresses = {
                network.get(key)
                for network in restarted["NetworkSettings"]["Networks"].values()
                for key in ("IPAddress", "GlobalIPv6Address")
            } - {None, ""}
            require(
                set(change["new"]) <= current_addresses,
                "Docker IP changed again; the single retry cannot establish readiness.",
            )
            self.wait_k3s(restarted, set(change["new"]))
            self.receipt(
                "node-ip-restart",
                {"container_id": node["Id"], "change": change, "retries": 1},
            )
        self.bound_node()

    def bound_node(self):
        self.ownership()
        node = self.service("k3s")
        config = node["HostConfig"]
        require(
            config["Memory"] == 4 * GIB
            and config["MemorySwap"] == 4 * GIB
            and config["NanoCpus"] == 4_000_000_000,
            "K3s resource configuration changed; refusing to replace it.",
        )
        self.command(
            [
                "docker",
                "update",
                "--memory",
                "4g",
                "--memory-swap",
                "4g",
                "--cpus",
                "4",
                node["Id"],
            ]
        )
        current = self.service("k3s")
        require(
            current["Id"] == node["Id"] and current["State"]["Pid"] == node["State"]["Pid"],
            "K3s changed while applying its resource bounds.",
        )
        container_cgroup(process_cgroup(current["State"]["Pid"]), current["Id"])

    def start(self):
        host = self.preflight()
        self.initialize(host)
        self.ownership()
        self.volume_guard()
        require(
            self.marker["sample_image"] == SAMPLE_IMAGE,
            "Sample source changed; review its image/scope migration before starting existing state.",
        )
        images = self.images()
        running = {
            item["Config"]["Labels"].get("com.docker.compose.service")
            for item in self.ownership()["container"]
            if item["State"]["Running"]
        }
        if running & set(APP_SERVICES):
            status = json.loads(
                self.dc("run", "--rm", "-T", "--no-deps", "dispatch-admin", "status")
            )
            if status["permit"] is not None:
                self.dispatch("enter-recovery")
            if "executor" in running:
                self.maintenance("enter_recovery")
            self.dc("stop", "worker", "observer", "api")
        for service in ("api", "https", "sample"):
            self.dc("build", service, timeout=600)
        if RECOVERY_LAB:
            require(
                self.marker.get("recovery_images") == RECOVERY_IMAGES,
                "Recovery sample source changed; review its qualification before starting.",
            )
            for variant, tag in RECOVERY_IMAGES.items():
                self.command(
                    [
                        "docker",
                        "build",
                        "--file",
                        "config/lab/Dockerfile.recovery",
                        "--build-arg",
                        f"SAMPLE_VARIANT={variant}",
                        "--label",
                        f"{OWNER_LABEL}={self.marker['owner']}",
                        "--tag",
                        tag,
                        ".",
                    ],
                    timeout=600,
                )
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "240",
            "db",
            timeout=300,
        )
        self.start_k3s()
        self.volume_guard(remember=True)
        archive = self.directory / "sample.tar"
        try:
            with archive.open("wb") as output:
                self.command(
                    [
                        "docker",
                        "save",
                        SAMPLE_IMAGE,
                        *(RECOVERY_IMAGES.values() if RECOVERY_LAB else ()),
                    ],
                    stdout=output,
                )
            with archive.open("rb") as source:
                result = subprocess.run(
                    [
                        *self.compose,
                        "exec",
                        "-T",
                        "k3s",
                        "/bin/ctr",
                        "images",
                        "import",
                        "-",
                    ],
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    cwd=ROOT,
                    env=self.env,
                    timeout=120,
                )
            require(result.returncode == 0, "Sample image import failed.")
        finally:
            archive.unlink(missing_ok=True)
        self.seed()
        if not (self.directory / "fault.json").exists():
            for name in ("checkout", "history"):
                self.kube(
                    "rollout",
                    "status",
                    "deployment/" + name,
                    "-n",
                    "demo",
                    "--timeout=180s",
                    timeout=200,
                )
        self.kube(
            "rollout",
            "status",
            "deployment/kube-state-metrics",
            "-n",
            "cloudops-lab-monitoring",
            "--timeout=180s",
            timeout=200,
        )
        for kind in ("reader", "writer", "monitoring"):
            if not (self.directory / kind / "expiry.json").exists():
                self.credential(kind)
        self.credentials_valid()
        self.tls_config()
        self.dc(*namespace_recreate_arguments())
        self.dc("run", "--rm", "-T", "--no-deps", "migrate", timeout=120)
        self.dc("run", "--rm", "-T", "--no-deps", "executor-init", timeout=120)
        self.volume_guard(remember=True)
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "120",
            *APP_SERVICES,
            timeout=180,
        )
        self.volume_guard(remember=True)
        self.dc("up", "-d", "--no-deps", "--force-recreate", "alertmanager")
        self.volume_guard(remember=True)
        status = json.loads(self.dc("run", "--rm", "-T", "--no-deps", "dispatch-admin", "status"))
        if status["permit"] is None:
            require(not status["audit"], "Dispatch audit exists; fresh bootstrap denied.")
            self.dc(
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "dispatch-admin",
                "bootstrap",
                "--fresh-install",
                "--reason",
                "Owned lab installation",
                "--evidence",
                "local://lab/start",
            )
        elif status["permit"]["mode"] == "recovery":
            for kind in ("collection", "recommendation"):
                self.dispatch("resume", "--class", kind)
        require(
            all(
                not target["enabled"] and target["recovery_mode"]
                for target in self.maintenance()["targets"]
            ),
            "Executor unexpectedly enabled; stop and inspect authority.",
        )
        self.receipt(
            "start",
            {
                **images,
                "runtime_images": self.image_receipts(),
                "stress": "requires a fresh verify/containment check",
                "accounts": "Provision explicitly through CLI; none seeded.",
            },
        )

    def resume(self):
        self.volume_guard()
        self.credentials_valid()
        require(
            self.marker["sample_image"] == SAMPLE_IMAGE,
            "Sample source changed; use reviewed setup instead of resume.",
        )
        services = ("db", "k3s", *APP_SERVICES, "https", "prometheus", "alertmanager")
        config = json.loads(self.dc("config", "--format", "json"))["services"]
        for name in services:
            image = json.loads(self.command(["docker", "image", "inspect", config[name]["image"]]))[
                0
            ]
            require(
                image["Id"] == self.service(name)["Image"]
                and image["Os"] == "linux"
                and image["Architecture"] == "amd64",
                "Retained image changed or is missing; resume cannot rebuild or substitute it.",
            )
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
            "db",
            timeout=150,
        )
        self.start_k3s(pull_missing=False)
        require(
            self.get("namespace", "demo")["metadata"]["uid"] == self.marker["namespace_uid"],
            "Namespace identity changed.",
        )
        for name in self.marker["deployments"]:
            self.deployment(name)
        from cloudops.features.workloads.scope_config import cluster_fingerprint

        ca = self.kube(
            "config",
            "view",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        ).strip()
        require(
            cluster_fingerprint({"server": "https://k3s:6443", "ca_data": ca})
            == self.marker["cluster_fingerprint"],
            "Cluster identity changed; resume cannot repin it.",
        )
        self.dc("up", "-d", "--no-deps", "--no-build", "--pull", "never", "https")
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--no-build",
            "--pull",
            "never",
            "prometheus",
        )
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
            *APP_SERVICES,
            timeout=150,
        )
        self.dispatch("enter-recovery")
        self.maintenance("enter_recovery")
        self.no_execution()
        for kind in ("collection", "recommendation"):
            self.dispatch("resume", "--class", kind)
        self.dc("up", "-d", "--no-deps", "--no-build", "--pull", "never", "alertmanager")
        self.namespace_check()
        self.receipt(
            "resume",
            {
                "images": "existing retained artifacts",
                "accounts": "preserved",
                "writer": "quarantined",
                "optional_services": "load and KRR are not started by resume",
            },
        )

    def service(self, name):
        ids = self.dc("ps", "-aq", name).split()
        require(len(ids) == 1, f"Need exactly one owned {name} container.")
        return json.loads(self.command(["docker", "inspect", ids[0]]))[0]

    def image_receipts(self):
        values = {}
        for service in (
            "k3s",
            "db",
            "api",
            "worker",
            "observer",
            "executor",
            "https",
            "prometheus",
            "alertmanager",
        ):
            container = self.service(service)
            try:
                image = json.loads(
                    self.command(["docker", "image", "inspect", container["Image"]])
                )[0]
                resolved_by = "runtime_id"
            except RuntimeError:
                # The containerd image store reports a container's config digest, which it
                # cannot inspect directly; resolve the exact reference the container uses.
                image = json.loads(
                    self.command(["docker", "image", "inspect", container["Config"]["Image"]])
                )[0]
                resolved_by = "reference"
            values[service] = {
                "image_id": container["Image"],
                "repo_digests": image.get("RepoDigests", []),
                **(
                    {"reference": container["Config"]["Image"], "image_store_id": image["Id"]}
                    if resolved_by == "reference"
                    else {}
                ),
            }
        return values

    def workload_images(self):
        pods = json.loads(self.kube("get", "pods", "-A", "-o", "json"))["items"]
        return [
            {
                "namespace": pod["metadata"]["namespace"],
                "pod_uid": pod["metadata"]["uid"],
                "containers": [
                    {"name": status["name"], "image_id": status.get("imageID")}
                    for status in pod.get("status", {}).get("containerStatuses", [])
                ],
            }
            for pod in pods
        ]

    def denials(self):
        self.no_execution()
        self.command([sys.executable, "scripts/probe-lab-credentials.py"])
        prefix = [
            "exec",
            "-T",
            "executor",
            "python",
            "-m",
            "cloudops.features.recovery.maintenance",
        ]
        before = self.maintenance()["targets"]
        invalid = subprocess.run(
            [*self.compose, *prefix, "enable_target"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=self.env,
            timeout=30,
        )
        require(
            invalid.returncode == 1
            and json.loads(invalid.stdout)["code"] == "maintenance_request_invalid",
            "Missing-field maintenance denial was not established.",
        )
        peer = subprocess.run(
            [
                *self.compose,
                "exec",
                "-T",
                "--user",
                "10001",
                "executor",
                "python",
                "-m",
                "cloudops.features.recovery.maintenance",
                "status",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=self.env,
            timeout=30,
        )
        require(
            peer.returncode == 1 and json.loads(peer.stdout)["code"] == "maintenance_actor_denied",
            "Untrusted-peer denial was not established.",
        )
        require(
            self.maintenance()["targets"] == before,
            "Denial checks changed executor authority.",
        )
        self.receipt(
            "denials",
            {
                "rbac_and_mounts": json.loads(
                    (self.directory / "credential-boundaries.json").read_text()
                ),
                "missing_fields_denied": True,
                "untrusted_peer_denied": True,
                "writer": "remains quarantined",
            },
        )

    def stored(self, password, create=False):
        spec = importlib.util.spec_from_file_location(
            "lab_e04", ROOT / "scripts/probe-e04-integration.py"
        )
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        path = self.directory / "stored-evidence.json"
        with probe.client("https://localhost:8443", self.directory / "prometheus/ca.crt") as client:
            headers = probe.sign_in(client, "https://localhost:8443", "operator", password)
            if path.exists():
                baseline = json.loads(path.read_text())
                run = client.get(
                    f"/api/incidents/{baseline['ids']['incident_id']}/runs/{baseline['ids']['run_id']}",
                    headers=headers,
                )
                run.raise_for_status()
                require(
                    probe.evidence(client, baseline["ids"], run.json(), headers)
                    == baseline["evidence"],
                    "Stored evidence IDs/content/digests changed; preserve the failed receipt.",
                )
            else:
                require(create, "Collect a baseline before checking persistence.")
            if create:
                workload = probe.workloads(client, headers)["checkout"]
                require(
                    workload["uid"] == self.marker["deployments"]["checkout"],
                    "Application workload UID differs from lab target.",
                )
                ids = probe.create_run(client, headers, workload, "Local workload observation")
                run = probe.wait_run(client, ids, headers)
                rows = probe.evidence(client, ids, run, headers)
                record = {
                    "ids": ids,
                    "evidence": rows,
                    "window": run["window"],
                    "pack_coverage": run["pack_coverage"],
                    "run_state": run["state"],
                    "log_comparison": self.compare_logs(client, headers, ids, run),
                }
                private_write(self.directory / "last-collection.json", json.dumps(record))
                if not path.exists():
                    private_write(path, json.dumps(record))
                self.receipt("collect", record)
            else:
                self.receipt("stored", {"ids": baseline["ids"], "evidence_unchanged": True})

    def compare_logs(self, client, headers, ids, run):
        pods = json.loads(self.kube("get", "pods", "-n", "demo", "-o", "json"))["items"]
        by_uid = {pod["metadata"]["uid"]: pod for pod in pods}
        comparisons = []
        for source in run["evidence"]:
            if (
                source["source_key"] not in {"kubernetes.logs.current", "kubernetes.logs.previous"}
                or source["content_status"] != "available"
            ):
                continue
            response = client.get(
                f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/evidence/{source['id']}/content",
                headers=headers,
            )
            response.raise_for_status()
            for entry in response.json()["payload"].get("logs", [])[:20]:
                pod = by_uid.get(entry["pod_uid"])
                if pod is None:
                    comparisons.append(
                        {
                            "pod_uid": entry["pod_uid"],
                            "outcome": "original_pod_unavailable",
                        }
                    )
                    continue
                self.pod_binding(pod)
                options = {
                    "container": entry["container"],
                    "follow": "false",
                    "timestamps": "true",
                    "tailLines": "200",
                    "previous": "true" if entry["previous"] else "false",
                    "sinceTime": run["window"]["start"],
                    "limitBytes": "32768",
                }
                raw = self.kube(
                    "get",
                    "--raw",
                    f"/api/v1/namespaces/demo/pods/{pod['metadata']['name']}/log?"
                    + urlencode(options),
                )
                current = self.get("pod", pod["metadata"]["name"], "demo")
                require(
                    current is not None and current["metadata"]["uid"] == entry["pod_uid"],
                    "Pod changed during raw/collector comparison.",
                )
                comparisons.append(
                    {
                        "pod_uid": entry["pod_uid"],
                        "container": entry["container"],
                        "options": options,
                        "stored_coverage": source["coverage"],
                        "raw_bytes": len(raw.encode()),
                        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        "equal_bytes": raw == entry["text"],
                        "raw_timestamped_nonempty": timestamped_nonempty(raw),
                        "limitation": "Raw read occurs after collection; container restarts and newer output can differ. Stored coverage is unchanged.",
                    }
                )
        return comparisons

    def replace_history(self):
        self.no_execution()
        resource = self.deployment("history")
        old = resource["metadata"]["uid"]
        query = 'lab_http_requests_total{deployment_uid="' + old + '"}'
        query_at = datetime.now(timezone.utc).isoformat()
        old_samples = self.prom("/api/v1/query?" + urlencode({"query": query, "time": query_at}))[
            "data"
        ]["result"]
        require(
            old_samples and any(float(row["value"][1]) > 0 for row in old_samples),
            "Collect history traffic samples before replacing its Deployment.",
        )
        require(
            len(self.marker.get("retired_history_uids", [])) < 10,
            "Replacement campaign bound reached.",
        )
        snapshot = {
            "uid": old,
            "query_at": query_at,
            "samples": old_samples,
        }
        private_write(self.directory / "history-replacement.json", json.dumps(snapshot))
        self.kube(
            "delete",
            "--raw",
            "/apis/apps/v1/namespaces/demo/deployments/history",
            "-f",
            "-",
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": old,
                    "resourceVersion": resource["metadata"]["resourceVersion"],
                },
            },
        )
        self.wait(
            lambda: self.get("deployment", "history", "demo") is None,
            "history deletion",
            60,
        )
        self.marker.setdefault("retired_history_uids", []).append(old)
        del self.marker["deployments"]["history"]
        self.save()
        self.seed()
        self.credential("reader")
        self.dc("run", "--rm", "-T", "--no-deps", "migrate", timeout=120)
        self.dc("restart", "api", "worker", "observer")
        new = self.marker["deployments"]["history"]
        require(new != old, "Replacement did not create a new UID.")
        retained = self.prom(
            "/api/v1/query?" + urlencode({"query": query, "time": snapshot["query_at"]})
        )["data"]["result"]
        require(retained == old_samples, "Historical samples changed during replacement.")
        self.receipt(
            "replace-history",
            {
                "old_uid": old,
                "new_uid": new,
                "old_samples_preserved": True,
                "writer_scope": "unchanged",
                "remaining": "Load history again, then verify current target/Pod/Deployment UID binding.",
            },
        )

    def maintenance(self, operation="status"):
        args = [
            "exec",
            "-T",
            "executor",
            "python",
            "-m",
            "cloudops.features.recovery.maintenance",
            operation,
        ]
        if operation != "status":
            args += [
                "--target",
                self.marker["deployments"]["checkout"],
                "--reason",
                "Owned lab lifecycle",
                "--evidence",
                "local://lab/lifecycle",
            ]
        return json.loads(self.dc(*args))

    def dispatch(self, operation, *extra):
        self.dc(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "dispatch-admin",
            operation,
            *extra,
            "--reason",
            "Owned lab lifecycle",
            "--evidence",
            "local://lab/lifecycle",
        )

    def no_execution(self):
        value = self.dc(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from sqlalchemy import text;from cloudops.shared.database import sessions\nwith sessions()() as db: print(db.execute(text(\"select count(*) from recovery_executions where holds_lock or state in ('queued','executing','verifying','reconciling','rolling_back')\")).scalar())",
        )
        require(int(value) == 0, "Active or unresolved execution; lab reset/fault refused.")
        status = self.maintenance()
        require(
            all(not target["enabled"] and target["recovery_mode"] for target in status["targets"])
            and not any(
                row.get("safety_exclusion_request") is None
                for row in status.get("unknown_attributions", [])
            ),
            "Writer must be quarantined with no unresolved old write; use supported maintenance, never force unlock.",
        )

    def stop(self):
        self.dispatch("enter-recovery")
        self.maintenance("enter_recovery")
        self.dc(
            "stop",
            "load",
            "alertmanager",
            "prometheus",
            *APP_SERVICES,
            "https",
            "krr",
            "db",
            "k3s",
            timeout=120,
        )
        self.receipt(
            "stop",
            {"volumes": "preserved", "dispatch": "closed", "writer": "quarantined"},
        )

    def recreate(self):
        self.no_execution()
        self.dispatch("enter-recovery")
        self.maintenance("enter_recovery")
        self.dc("stop", "prometheus", "worker", "observer")
        self.start_k3s(recreate=True)
        self.dc(*namespace_recreate_arguments())
        self.dc("up", "-d", "--no-deps", "worker", "observer")
        for kind in ("collection", "recommendation"):
            self.dispatch("resume", "--class", kind)
        self.receipt(
            "recreate",
            {
                "data": "preserved",
                "network_attachment": self.namespace_check(),
                "remaining": "Run verify to prove fresh discovery and kubelet scrape.",
            },
        )

    def namespace_check(self):
        node = self.service("k3s")
        prom = self.service("prometheus")
        require(
            prom["HostConfig"]["NetworkMode"] == "container:" + node["Id"],
            "Prometheus attached to a different K3s network namespace; recreate it.",
        )
        require(
            not prom["HostConfig"].get("Privileged")
            and prom["HostConfig"].get("PidMode") != "host",
            "Monitoring isolation changed.",
        )
        return {"k3s_id": node["Id"], "prometheus_id": prom["Id"], "attached": True}

    def containment(self, *, read_only=False):
        if read_only:
            self.credentials_valid(read_only=True)
        else:
            self.credentials_valid()
        node = self.service("k3s")
        config = node["HostConfig"]
        require(
            config["Memory"] == 4 * GIB
            and config["MemorySwap"] == config["Memory"]
            and config["NanoCpus"] == 4_000_000_000,
            "K3s Docker memory/swap/CPU containment is not bounded.",
        )
        pid = node["State"]["Pid"]
        require(pid > 0, "K3s is not running.")
        subgroup = process_cgroup(pid)
        parent = container_cgroup(subgroup, node["Id"])
        memory = (parent / "memory.max").read_text().strip()
        quota, period = (parent / "cpu.max").read_text().split()
        require(
            memory != "max"
            and int(memory) <= 4 * GIB
            and (parent / "memory.swap.max").read_text().strip() == "0"
            and quota != "max"
            and int(quota) / int(period) <= 4,
            "Effective K3s cgroup limits are missing.",
        )
        nodes = json.loads(self.kube("get", "nodes", "-o", "json"))["items"]
        require(
            len(nodes) == 1
            and not nodes[0].get("spec", {}).get("taints")
            and not nodes[0].get("spec", {}).get("unschedulable"),
            "Need one schedulable untainted node.",
        )
        conditions = {item["type"]: item["status"] for item in nodes[0]["status"]["conditions"]}
        require(
            conditions.get("Ready") == "True"
            and all(
                conditions.get(key) == "False"
                for key in ("MemoryPressure", "PIDPressure", "DiskPressure")
            ),
            "Node is not healthy without pressure.",
        )
        from decimal import Decimal
        from cloudops.features.workloads.kubernetes import pod_requests, quantity

        alloc = nodes[0]["status"]["allocatable"]
        cpu = (
            Decimal(alloc["cpu"][:-1]) / 1000
            if alloc["cpu"].endswith("m")
            else Decimal(alloc["cpu"])
        )
        require(
            cpu <= 3 and quantity(alloc["memory"]) <= 3 * GIB,
            "Node advertises unavailable host resources; review supported kubelet reserves. Never patch Node capacity.",
        )
        pods = json.loads(self.kube("get", "pods", "-n", "demo", "-o", "json"))["items"]
        descendants = {}
        for file in parent.rglob("cgroup.procs"):
            try:
                for host_pid in file.read_text().split():
                    descendants[int(host_pid)] = file.parent
            except FileNotFoundError:
                continue
        proof = []
        sample_limits = {"cpu": Decimal(0), "memory": Decimal(0)}
        for name in ("checkout", "history"):
            self.deployment(name)
            candidates = [
                pod
                for pod in pods
                if pod["metadata"].get("labels", {}).get("app") == name
                and pod["status"].get("phase") == "Running"
            ]
            require(
                candidates,
                "Need healthy sample Pods to prove containment before faults.",
            )
            for pod in candidates:
                self.pod_binding(pod)
                for spec in [
                    *pod["spec"]["containers"],
                    *pod["spec"].get("initContainers", []),
                ]:
                    limits = spec.get("resources", {}).get("limits", {})
                    require(
                        quantity(limits.get("cpu", "1000")) <= Decimal("0.4")
                        and quantity(limits.get("memory", str(4 * GIB))) <= SAMPLE_MEMORY_ENVELOPE,
                        "Sample limits exceed the approved workload envelope.",
                    )
                    for resource in sample_limits:
                        sample_limits[resource] += quantity(limits[resource])
                for container in pod["status"].get("containerStatuses", []):
                    require(
                        container.get("state", {}).get("running"),
                        "Need running sample container for fresh containment proof.",
                    )
                    cid = container["containerID"].removeprefix("containerd://")
                    details = json.loads(
                        self.dc("exec", "-T", "k3s", "/bin/crictl", "inspect", cid)
                    )
                    inner_pid = int(details["info"]["pid"])
                    require(
                        details["status"]["id"] == cid
                        and details["status"].get("labels", {}).get("io.kubernetes.pod.uid")
                        == pod["metadata"]["uid"],
                        "CRI container identity does not match the sample Pod; stress blocked.",
                    )
                    matches = []
                    for host_pid, path in descendants.items():
                        try:
                            status = Path(f"/proc/{host_pid}/status").read_text()
                            nspid = next(
                                line.split()[1:]
                                for line in status.splitlines()
                                if line.startswith("NSpid:")
                            )
                            if len(nspid) >= 2 and int(nspid[1]) == inner_pid and path != parent:
                                matches.append((host_pid, path))
                        except (FileNotFoundError, ProcessLookupError, StopIteration):
                            continue
                    require(
                        len(matches) == 1,
                        "Pod process does not descend from the limited K3s cgroup; stress blocked.",
                    )
                    container_path = pod_cgroup(matches[0][0], matches[0][1], parent)
                    limit = (container_path / "memory.max").read_text().strip()
                    require(
                        limit != "max" and int(limit) <= SAMPLE_MEMORY_ENVELOPE,
                        "Sample container memory is not bounded.",
                    )
                    pod_quota, pod_period = (container_path / "cpu.max").read_text().split()
                    require(
                        pod_quota != "max" and int(pod_quota) / int(pod_period) <= 0.4,
                        "Sample container CPU is not bounded.",
                    )
                    proof.append(
                        {
                            "pod_uid": pod["metadata"]["uid"],
                            "container": container["name"],
                            "descends_from_k3s": True,
                            "memory_max": int(limit),
                        }
                    )
        require(len(proof) <= 6, "Unexpected sample concurrency; stress blocked.")
        all_pods = json.loads(self.kube("get", "pods", "-A", "-o", "json"))["items"]
        other_requests = {"cpu": Decimal(0), "memory": Decimal(0)}
        proven = {item["pod_uid"] for item in proof}
        for pod in all_pods:
            if (
                pod["metadata"]["uid"] not in proven
                and pod.get("spec", {}).get("nodeName") == nodes[0]["metadata"]["name"]
                and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            ):
                requests = pod_requests(pod["spec"])
                for resource in other_requests:
                    other_requests[resource] += requests[resource]
        require(
            all(
                sample_limits[resource] + other_requests[resource] <= quantity(alloc[resource])
                for resource in sample_limits
            ),
            "Sample limits and other scheduled requests exceed proven node allocatable; stress blocked.",
        )
        require(
            shutil.disk_usage(ROOT).free >= 5 * GIB,
            "Low disk headroom; stress blocked.",
        )
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        require(available >= 2 * GIB, "Low host memory headroom; stress blocked.")
        current = self.service("k3s")
        require(
            current["Id"] == node["Id"]
            and current["State"]["Pid"] == pid
            and process_cgroup(pid) == subgroup,
            "K3s identity changed during containment inspection; stress blocked.",
        )
        return {
            "pid_cgroup": str(subgroup),
            "container_cgroup": str(parent),
            "effective_memory_max": int(memory),
            "effective_cpu_max": int(quota) / int(period),
            "allocatable": alloc,
            "pods": proof,
        }

    def prom(self, path):
        return json.loads(
            self.dc(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://prometheus:9090'+"
                + repr(path)
                + ",timeout=8).read(2000000).decode())",
            )
        )

    def verify(self, finite_uids=None):
        # Trial operations prove the fixed target; an idle peer may have a 0/0 ratio.
        finite = set(finite_uids or self.marker["deployments"].values())
        self.credentials_valid()
        self.dc(
            "exec",
            "-T",
            "prometheus",
            "promtool",
            "check",
            "config",
            "/etc/prometheus/prometheus.yml",
        )
        self.dc(
            "exec",
            "-T",
            "-w",
            "/etc/prometheus",
            "prometheus",
            "promtool",
            "test",
            "rules",
            "rules.test.yml",
        )
        binding = self.namespace_check()
        targets = self.prom("/api/v1/targets")["data"]["activeTargets"]
        offered_targets = [
            target for target in targets if target["labels"]["job"] == "lab-offered-load"
        ]
        required_targets = [
            target for target in targets if target["labels"]["job"] != "lab-offered-load"
        ]
        require(
            required_targets and all(target["health"] == "up" for target in required_targets),
            "A discovery/kubelet/metrics target is missing or unhealthy; inspect private Prometheus target diagnostics.",
        )
        if self.active_loads():
            require(
                offered_targets and all(target["health"] == "up" for target in offered_targets),
                "The active recovery offered-load source is not being scraped.",
            )
        require(
            {"sample", "kubelet", "kube-state-metrics"}
            <= {target["labels"]["job"] for target in targets},
            "Missing telemetry job.",
        )
        for target in targets:
            if target["labels"]["job"] == "sample":
                labels = target["labels"]
                require(
                    labels.get("deployment_uid")
                    == self.marker["deployments"].get(labels.get("deployment"))
                    and labels.get("pod_uid"),
                    "Telemetry has an invalid UID binding.",
                )
                pod = self.get("pod", labels["pod"], "demo")
                require(
                    pod is not None and pod["metadata"]["uid"] == labels["pod_uid"],
                    "Scrape target Pod identity changed.",
                )
                self.pod_binding(pod)
        queries = {}
        for metric in (
            "cloudops_requests_per_second",
            "cloudops_error_ratio",
            "cloudops_p95_latency_seconds",
            "cloudops_memory_working_set_bytes",
            "cloudops_memory_limit_bytes",
            "cloudops_restarts_total",
        ):
            rows = self.prom("/api/v1/query?" + urlencode({"query": metric}))["data"]["result"]
            require(len(rows) <= 25, "Telemetry query exceeded the 25-series source bound.")
            allowed = set(self.marker["deployments"].values()) | set(
                self.marker.get("retired_history_uids", [])
            )
            require(
                rows and all(row["metric"].get("deployment_uid") in allowed for row in rows),
                "Missing or incorrectly bound metric series.",
            )
            require(
                set(self.marker["deployments"].values())
                <= {row["metric"].get("deployment_uid") for row in rows},
                "A current workload has no metric series.",
            )
            require(
                all(
                    math.isfinite(float(row["value"][1]))
                    for row in rows
                    if row["metric"].get("deployment_uid") in finite
                ),
                "Metric has no finite observation; load the target and warm the window.",
            )
            queries[metric] = [{"labels": row["metric"], "value": row["value"]} for row in rows]
        result = {
            "network_attachment": binding,
            "targets": [
                {"job": target["labels"]["job"], "health": target["health"]} for target in targets
            ],
            "metrics": queries,
            "credential_expiry": {
                kind: json.loads((self.directory / kind / "expiry.json").read_text())
                for kind in ("reader", "writer", "monitoring")
            },
            "runtime_images": self.image_receipts(),
            "workload_images": self.workload_images(),
            "kubernetes_version": json.loads(self.kube("version", "-o", "json")),
            "remaining": "Containment must pass immediately before load/OOM/executor tests; model/recommendation quality is not established.",
        }
        self.receipt("verify", result)
        return result

    def readiness_load(self, seconds=40, rate=5):
        """Offer short fresh traffic so the target's health is measured, never assumed."""
        require(not self.active_loads(), "A targeted load is already running.")
        result = json.loads(
            self.dc(
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "load",
                "--target",
                "checkout",
                "--seconds",
                str(seconds),
                "--rate",
                str(rate),
                timeout=seconds + 60,
            )
        )
        require(
            result.get("completed_nominal_load") is True,
            "The bounded readiness load did not complete.",
        )
        return {
            "target": "checkout",
            "seconds": seconds,
            "rate": rate,
            "started_at": result["started_at"],
            "finished_at": result["finished_at"],
            "responses": result["responses"],
        }

    def wait_settled(self, seconds=180):
        """Wait until the target's old Pods are gone, not merely until the rollout reports done."""

        def settled():
            deployment = self.deployment("checkout")
            pods = json.loads(
                self.kube("get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json")
            )["items"]
            return len(pods) == int(deployment["spec"].get("replicas", 1)) and not any(
                pod["metadata"].get("deletionTimestamp") for pod in pods
            )

        self.kube(
            "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s", timeout=200
        )
        self.wait(settled, "settled target Pods", seconds)

    def trial_target_uids(self):
        return {self.marker["deployments"]["checkout"]}

    def active_loads(self):
        return [
            item
            for item in self.resources()["container"]
            if item["State"]["Running"]
            and item.get("Config", {}).get("Labels", {}).get("com.docker.compose.service") == "load"
        ]

    def source_binding(self):
        paths = (
            self.directory / "reader/scope.json",
            self.directory / "prometheus/ca.crt",
            ROOT / "config/lab/prometheus-real.yaml",
            ROOT / "config/lab/monitoring.yaml",
            ROOT / "config/lab/metrics_fixture.py",
            ROOT / "config/lab/Dockerfile.sample",
            ROOT / "config/lab/recovery_load.py",
        )
        if RECOVERY_LAB:
            # The qualified sample and the trusted E07 configuration are part of the binding.
            paths += tuple(
                ROOT / file
                for file in RECOVERY_SOURCES
                if file not in {"config/lab/recovery_load.py"}
            )
            paths += tuple(
                path
                for path in (
                    self.directory / "recovery/policy.json",
                    self.directory / "recovery/verification.json",
                )
                if path.exists()
            )
        require(
            all(path.is_file() and not path.is_symlink() for path in paths),
            "Exact reader/metrics source binding is incomplete.",
        )
        files = {
            path.relative_to(ROOT).as_posix()
            if path.is_relative_to(ROOT)
            else "owned/" + path.name: sha256_file(path)
            for path in paths
        }
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        return {
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "scope_manifest_sha256": files[paths[0].relative_to(ROOT).as_posix()],
            "files": files,
        }

    def healthy_target(self, deployment):
        uid = deployment["metadata"]["uid"]
        desired = int(deployment["spec"].get("replicas", 1))
        status = deployment.get("status", {})
        require(
            status.get("availableReplicas", 0) == desired
            and status.get("readyReplicas", 0) == desired,
            "Target Deployment is not fully ready.",
        )
        pods = json.loads(
            self.kube("get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json")
        )["items"]
        require(len(pods) == desired, "Target Pod count differs from the desired replicas.")
        image_ids = []
        pod_ids = []
        for pod in pods:
            require(
                pod.get("status", {}).get("phase") == "Running"
                and not pod["metadata"].get("deletionTimestamp"),
                "Target Pod is not running or is terminating.",
            )
            self.pod_binding(pod)
            statuses = pod.get("status", {}).get("containerStatuses", [])
            require(
                statuses and all(item.get("ready") for item in statuses),
                "Target has a container that is not ready.",
            )
            for item in statuses:
                image_id = item.get("imageID")
                require(image_id, "Target runtime image identity is unavailable.")
                image_ids.append(image_id)
            pod_ids.append(pod["metadata"]["uid"])
        return {
            "target_uid": uid,
            "pod_uids": pod_ids,
            "image_digests": sorted(set(image_ids)),
        }

    def surge_headroom(self, deployment):
        from decimal import Decimal

        from cloudops.features.workloads.kubernetes import pod_requests, quantity

        nodes = json.loads(self.kube("get", "nodes", "-o", "json"))["items"]
        require(
            len(nodes) == 1,
            "Scenario qualification requires the recorded single-node lab.",
        )
        allocatable = nodes[0]["status"]["allocatable"]
        cpu_alloc = (
            Decimal(allocatable["cpu"][:-1]) / 1000
            if allocatable["cpu"].endswith("m")
            else Decimal(allocatable["cpu"])
        )
        memory_alloc = quantity(allocatable["memory"])
        cpu_used = Decimal(0)
        memory_used = Decimal(0)
        pods = json.loads(self.kube("get", "pods", "-A", "-o", "json"))["items"]
        for pod in pods:
            if pod.get("spec", {}).get("nodeName") == nodes[0]["metadata"]["name"] and pod.get(
                "status", {}
            ).get("phase") not in {"Succeeded", "Failed"}:
                requests = pod_requests(pod["spec"])
                cpu_used += requests["cpu"]
                memory_used += requests["memory"]

        strategy = deployment["spec"].get("strategy", {})
        rolling = strategy.get("rollingUpdate", {})
        raw_surge = rolling.get("maxSurge", 1)
        replicas = int(deployment["spec"].get("replicas", 1))
        if isinstance(raw_surge, str) and raw_surge.endswith("%"):
            surge_replicas = math.ceil(replicas * int(raw_surge[:-1]) / 100)
        else:
            surge_replicas = int(raw_surge)
        target_request = pod_requests(deployment["spec"]["template"]["spec"])
        available_cpu = max(Decimal(0), cpu_alloc - cpu_used)
        available_memory = max(0, memory_alloc - memory_used)
        required_cpu = target_request["cpu"] * surge_replicas
        required_memory = target_request["memory"] * surge_replicas
        return {
            "required_cpu_millicores": math.ceil(required_cpu * 1000),
            "available_cpu_millicores": math.floor(available_cpu * 1000),
            "required_memory_bytes": int(required_memory),
            "available_memory_bytes": int(available_memory),
            "qualified": available_cpu >= required_cpu and available_memory >= required_memory,
        }

    def recovery_surge_demand(self, deployment, action_state):
        from cloudops.features.workloads.kubernetes import pod_requests

        spec = {
            "containers": [{"resources": action_state["template"]["resources"]}],
            "initContainers": action_state["template"]["initContainers"] or [],
        }
        request = pod_requests(spec)
        replicas = action_state["replicas"]
        baseline_replicas = int(deployment["spec"].get("replicas", 1))
        rolling = action_state["rolling_update"]
        raw_surge = rolling.get("maxSurge", 1)
        if isinstance(raw_surge, str) and raw_surge.endswith("%"):
            surge_replicas = math.ceil(replicas * int(raw_surge[:-1]) / 100)
        else:
            surge_replicas = int(raw_surge)
        additional_replicas = max(0, replicas - baseline_replicas)
        surge_units = additional_replicas + surge_replicas
        return {
            "required_cpu_millicores": math.ceil(request["cpu"] * surge_units * 1000),
            "required_memory_bytes": int(request["memory"] * surge_units),
        }

    def recovery_surge_headroom(self, deployment, action_state):
        headroom = self.surge_headroom(deployment)
        demand = self.recovery_surge_demand(deployment, action_state)
        headroom["required_cpu_millicores"] = max(
            headroom["required_cpu_millicores"], demand["required_cpu_millicores"]
        )
        headroom["required_memory_bytes"] = max(
            headroom["required_memory_bytes"], demand["required_memory_bytes"]
        )
        headroom["qualified"] = (
            headroom["available_cpu_millicores"] >= headroom["required_cpu_millicores"]
            and headroom["available_memory_bytes"] >= headroom["required_memory_bytes"]
        )
        return headroom, demand

    def eligible_action_count(self):
        value = self.dc(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from sqlalchemy import text;from cloudops.shared.database import sessions\n"
            "with sessions()() as db: print(db.execute(text(\"select count(*) from action_plans where state in ('proposed','approved') and expires_at > now()\")).scalar())",
        )
        return int(value)

    def target_metric_snapshot(self, verification, uid):
        result = {}
        now = time.time()
        for metric, rows in verification["metrics"].items():
            target_rows = [row for row in rows if row["labels"].get("deployment_uid") == uid]
            require(
                target_rows,
                f"Current application metric {metric} is not bound to the target UID.",
            )
            newest = max(target_rows, key=lambda row: float(row["value"][0]))
            observed = float(newest["value"][0])
            require(
                0 <= now - observed <= 120,
                f"Current application metric {metric} is stale.",
            )
            result[metric] = {
                "timestamp": newest["value"][0],
                "value": newest["value"][1],
            }
        return result

    def offered_rate_snapshot(self, uid, expected_rate):
        selector = f'namespace="demo",deployment_uid="{uid}"'
        rate_rows = self.prom(
            "/api/v1/query?"
            + urlencode(
                {
                    "query": f"sum by(namespace,deployment_uid) (rate(lab_offered_requests_total{{{selector}}}[30s]))"
                }
            )
        )["data"]["result"]
        sample_rows = self.prom(
            "/api/v1/query?"
            + urlencode({"query": f"timestamp(lab_offered_requests_total{{{selector}}})"})
        )["data"]["result"]
        require(
            len(rate_rows) <= 25 and len(sample_rows) <= 25,
            "Offered-load telemetry exceeded the 25-series source bound.",
        )
        require(
            len(rate_rows) == 1 and len(sample_rows) == 1,
            "Offered-load telemetry is missing or ambiguous for the exact target UID.",
        )
        rate_row = rate_rows[0]
        sample_row = sample_rows[0]
        require(
            rate_row["metric"].get("namespace") == "demo"
            and rate_row["metric"].get("deployment_uid") == uid
            and sample_row["metric"].get("namespace") == "demo"
            and sample_row["metric"].get("deployment_uid") == uid,
            "Offered-load telemetry is not bound to the exact target UID.",
        )
        observed_rate = float(rate_row["value"][1])
        sample_at = float(sample_row["value"][1])
        now = time.time()
        require(
            math.isfinite(observed_rate)
            and expected_rate * 0.8 <= observed_rate <= expected_rate * 1.2,
            "Observed offered rate differs from the fixed trial rate.",
        )
        require(0 <= now - sample_at <= 120, "Offered-load source sample is stale.")
        return {
            "namespace": "demo",
            "deployment_uid": uid,
            "expected_rate_rps": expected_rate,
            "observed_rate_rps": observed_rate,
            "sample_at": sample_row["value"][1],
        }

    def trial_rollout_parameters_match_baseline(self, trial, deployment):
        expected = trial["declared_resources"]
        strategy = deployment["spec"].get("strategy", {})
        return (
            deployment["spec"].get("replicas", 1) == expected["replicas"]
            and strategy.get("rollingUpdate", {}) == expected["rolling_update"]
        )

    def trial_rollout_parameters_are_allowed(self, trial, deployment):
        baseline = trial["declared_resources"]
        strategy = deployment["spec"].get("strategy", {})
        allowed_replicas = {baseline["replicas"]}
        qualification = trial.get("qualification")
        if qualification:
            allowed_replicas.add(qualification["declared_resources"]["replicas"])
        return (
            deployment["spec"].get("replicas", 1) in allowed_replicas
            and strategy.get("rollingUpdate", {}) == baseline["rolling_update"]
        )

    def validate_qualified_recovery_action(self, scenario_id, deployment, action_state):
        from decimal import Decimal

        from cloudops.features.workloads.kubernetes import quantity
        from cloudops.shared.errors import DomainError

        require(
            RECOVERY_LAB,
            "Recovery scenarios need the E10 S3 recovery lab and its qualified sample.",
        )
        baseline_template = owned_fields(deployment)
        template = action_state["template"]
        # Every action returns to the captured healthy image; only the field it repairs
        # (replicas or bounded memory) may differ from the healthy baseline.
        require(
            template["image"] == baseline_template["image"]
            and template["imagePullPolicy"] == baseline_template["imagePullPolicy"]
            and template["initContainers"] == baseline_template["initContainers"]
            and template["mode"] == baseline_template["mode"],
            "Qualified recovery may not change the captured image, pull policy, mode or init containers.",
        )
        baseline_replicas = int(deployment["spec"].get("replicas", 1))
        require(
            1 <= action_state["replicas"] <= 3,
            "Qualified scale exceeds the owned sample replica bound.",
        )
        if scenario_id == "recovery-traffic-scale":
            require(
                action_state["replicas"] > baseline_replicas
                and template["resources"] == baseline_template["resources"],
                "Qualified traffic recovery must scale within the captured resource profile.",
            )
        elif scenario_id == "recovery-bad-rollout-restore":
            require(
                action_state["replicas"] == baseline_replicas
                and template["resources"] == baseline_template["resources"],
                "Qualified rollout restore must return to the captured healthy resources.",
            )
        else:
            require(
                action_state["replicas"] == baseline_replicas,
                "Qualified memory recovery must retain the captured replica count.",
            )
            resources = template["resources"]
            require(
                set(resources) == {"requests", "limits"}
                and set(resources["requests"]) == {"cpu", "memory"}
                and set(resources["limits"]) == {"cpu", "memory"},
                "Qualified memory resources must declare bounded CPU and memory requests/limits.",
            )
            try:
                cpu_request = quantity(resources["requests"]["cpu"])
                cpu_limit = quantity(resources["limits"]["cpu"])
                memory_request = quantity(resources["requests"]["memory"])
                memory_limit = quantity(resources["limits"]["memory"])
            except DomainError:
                raise RuntimeError("Qualified memory resources are malformed.") from None
            require(
                0 < cpu_request <= cpu_limit <= Decimal("1")
                and 0 < memory_request <= memory_limit <= 512 * 1024**2,
                "Qualified memory profile exceeds the local CPU/memory bounds.",
            )
            require(
                memory_limit > quantity(FAULT_MEMORY_LIMIT),
                "Qualified memory profile does not lift the injected memory limit.",
            )

    def trial_spec_matches_baseline(self, trial, deployment):
        return (
            self.trial_rollout_parameters_match_baseline(trial, deployment)
            and owned_fields(deployment) == trial["declared_resources"]["template"]
        )

    def begin_trial(self, scenario_id, qualification_path=None):
        validate_catalog()
        catalog = load_catalog()
        scenarios = {item["id"]: item for item in catalog["scenarios"]}
        require(scenario_id in scenarios, "Unknown E10 S3 scenario ID.")
        scenario = scenarios[scenario_id]
        require(
            scenario["kind"] in {"recovery", "observation"},
            "Fixture-only negative cases do not start a live lab trial.",
        )
        require(
            scenario["kind"] != "recovery" or RECOVERY_LAB,
            "Recovery scenarios need the E10 S3 recovery lab and its qualified sample.",
        )
        require(
            not self.trial_path.exists(),
            "An unfinished trial receipt must be reset first.",
        )
        require(
            not (self.directory / "fault.json").exists(),
            "An unbound fault is present; inspect it before starting a trial.",
        )
        require(
            not self.active_loads(),
            "A targeted load is running; stop it before a trial.",
        )
        self.no_execution()
        require(
            self.eligible_action_count() == 0,
            "An unexpired eligible action exists; retire it through the supported path first.",
        )
        containment = self.containment(read_only=True)
        readiness = self.readiness_load()
        verification = self.verify(self.trial_target_uids())
        deployment = self.deployment("checkout")
        health = self.healthy_target(deployment)
        metrics = self.target_metric_snapshot(verification, health["target_uid"])
        source = self.source_binding()
        headroom = self.surge_headroom(deployment)
        owned = owned_fields(deployment)
        resources = {
            "replicas": deployment["spec"].get("replicas", 1),
            "template": owned,
            "rolling_update": deployment["spec"].get("strategy", {}).get("rollingUpdate", {}),
        }
        qualification = None
        status = "ready"
        manual_reason = None
        if scenario["qualification_required"]:
            if qualification_path is None:
                status = "manual"
                manual_reason = "No exact target/source/image qualification receipt was supplied."
            else:
                qualification_path = Path(qualification_path).resolve(strict=True)
                qualification = validate_qualification_receipt(read_json(qualification_path))
                require(
                    qualification.get("scenario_id") == scenario_id,
                    "Qualification scenario does not match the selected trial.",
                )
                require(
                    qualification.get("qualified") is True,
                    "Recovery target is not qualified; keep this case manual.",
                )
                require(
                    qualification.get("target_uid") == health["target_uid"],
                    "Qualification target UID does not match the live target.",
                )
                require(
                    qualification.get("source_binding_sha256") == source["sha256"],
                    "Qualification source binding does not match the live sources.",
                )
                require(
                    set(health["image_digests"]) == set(qualification.get("image_digests", [])),
                    "Qualification does not include the exact current runtime image IDs.",
                )
                profile_digest = qualification["profile_digest"]
                declared = qualification.get("declared_resources")
                require(
                    isinstance(declared, dict) and declared,
                    "Qualified desired resources are absent.",
                )
                self.validate_qualified_recovery_action(scenario_id, deployment, declared)
                qualified_headroom = qualification.get("surge_headroom", {})
                required_cpu = qualified_headroom.get("required_cpu_millicores")
                required_memory = qualified_headroom.get("required_memory_bytes")
                require(
                    isinstance(required_cpu, int) and isinstance(required_memory, int),
                    "Qualified surge demand is malformed.",
                )
                require(
                    declared["rolling_update"] == resources["rolling_update"],
                    "Qualified recovery action may not change the recorded rollout strategy.",
                )
                demand = self.recovery_surge_demand(deployment, declared)
                require(
                    required_cpu == demand["required_cpu_millicores"]
                    and required_memory == demand["required_memory_bytes"],
                    "Qualification surge demand does not match the exact declared resources.",
                )
                headroom["required_cpu_millicores"] = max(
                    headroom["required_cpu_millicores"], required_cpu
                )
                headroom["required_memory_bytes"] = max(
                    headroom["required_memory_bytes"], required_memory
                )
                headroom["qualified"] = (
                    headroom["available_cpu_millicores"] >= headroom["required_cpu_millicores"]
                    and headroom["available_memory_bytes"] >= headroom["required_memory_bytes"]
                )
                if not headroom["qualified"]:
                    status = "manual"
                    manual_reason = "Measured headroom is below the qualified action demand."
                resources["qualified_action"] = declared
                resources["qualification_profile_digest"] = profile_digest
                qualification = {
                    "path_sha256": sha256_file(qualification_path),
                    "profile_digest": profile_digest,
                    "declared_resources": declared,
                    "required_surge_cpu_millicores": required_cpu,
                    "required_surge_memory_bytes": required_memory,
                }
        if not headroom["qualified"]:
            status = "manual"
            manual_reason = manual_reason or "Measured rollout surge headroom is insufficient."
        now = datetime.now(timezone.utc).isoformat()
        trial = {
            "schema": "E10S3TrialState/v1",
            "trial_id": str(uuid4()),
            "run_id": str(uuid4()),
            "window_id": str(uuid4()),
            "reset_id": None,
            "scenario_id": scenario_id,
            "scenario_kind": scenario["kind"],
            "status": status,
            "manual_reason": manual_reason,
            "catalog_sha256": sha256_file(ROOT / "config/lab/release-scenarios-v1.json"),
            "scope": {
                "project": OWNED_PROJECT,
                "namespace": "demo",
                "scope_manifest_sha256": source["scope_manifest_sha256"],
            },
            "target_uid": health["target_uid"],
            "source_binding_sha256": source["sha256"],
            "source_files": source["files"],
            "image_digests": health["image_digests"],
            "declared_resources": resources,
            "surge_headroom": headroom,
            "baseline_at": now,
            "observed_at": now,
            "source_limits": {
                "request_timeout_seconds": 8,
                "response_bytes": 2_000_000,
                "series_limit": 25,
            },
            "residual_eligible_actions": 0,
            "duplicate_targeted_loads": 0,
            "baseline": {
                "pod_uids": health["pod_uids"],
                "owned_fields": owned,
                "metrics": metrics,
                "containment": containment,
                "readiness_load": readiness,
            },
            "qualification": qualification,
            "phases": {
                "baseline": now,
                "inject": None,
                "detect": None,
                "collect": now,
                "analyze": None,
                "approval": None,
                "apply": None,
                "verify": None,
                "reset": None,
            },
        }
        private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
        self.receipt(
            "scenario-readiness",
            {
                "trial_id": trial["trial_id"],
                "run_id": trial["run_id"],
                "window_id": trial["window_id"],
                "scenario_id": scenario_id,
                "status": status,
                "manual_reason": manual_reason,
                "target_uid": trial["target_uid"],
                "source_binding_sha256": trial["source_binding_sha256"],
                "image_digests": trial["image_digests"],
                "declared_resources": resources,
                "surge_headroom": headroom,
                "metrics": metrics,
                "active_targeted_loads": 0,
                "residual_eligible_actions": 0,
            },
        )
        return trial

    def start_offered_load(self, name, seconds, rate, uid):
        """Start the finite owned offered-load exporter bound to one Deployment UID."""
        container_id = self.dc(
            "run",
            "--detach",
            "--no-deps",
            "--use-aliases",
            "--name",
            name,
            "--entrypoint",
            "python",
            "load",
            "/app/recovery_load.py",
            "--seconds",
            str(seconds),
            "--rate",
            str(rate),
            "--deployment-uid",
            uid,
        ).strip()
        require(
            re.fullmatch(r"[a-f0-9]{12,64}", container_id) is not None,
            "Load did not return a container identity.",
        )
        container = json.loads(self.command(["docker", "inspect", container_id]))[0]
        labels = container.get("Config", {}).get("Labels", {})
        require(
            labels.get(OWNER_LABEL) == self.marker["owner"]
            and labels.get("com.docker.compose.project") == OWNED_PROJECT
            and labels.get("com.docker.compose.service") == "load"
            and labels.get("com.docker.compose.project.working_dir") == str(ROOT),
            "Recovery load container is outside the owned project.",
        )
        return container_id, container

    def recovery_healthy_fields(self):
        return {
            "image": "docker.io/library/" + RECOVERY_IMAGES["healthy"],
            "resources": RECOVERY_RESOURCES,
            "imagePullPolicy": "Never",
            "mode": None,
            "initContainers": None,
        }

    def require_idle_recovery_lab(self):
        require(RECOVERY_LAB, "This operation belongs to the E10 S3 recovery lab.")
        require(not self.trial_path.exists(), "Reset the active trial first.")
        require(not (self.directory / "fault.json").exists(), "Reset the recorded fault first.")
        require(not self.active_loads(), "A targeted load is running.")
        self.no_execution()
        require(self.eligible_action_count() == 0, "An unexpired eligible action exists.")

    def recovery_config(self):
        """Write the trusted E07 policy and verification profiles for the healthy target."""
        self.require_idle_recovery_lab()
        deployment = self.deployment("checkout")
        require(
            owned_fields(deployment) == self.recovery_healthy_fields()
            and int(deployment["spec"].get("replicas", 1)) == 1
            and deployment["spec"].get("strategy", {}).get("type") == "Recreate",
            "Configure recovery only from the captured healthy one-Pod recovery sample.",
        )
        health = self.healthy_target(deployment)
        require(len(health["image_digests"]) == 1, "Expected one runtime image identity.")
        image_id = health["image_digests"][0].rsplit("@", 1)[-1]
        uid = health["target_uid"]
        queries = {
            "request_rate": (
                "requests/s",
                "sum by(namespace,deployment_uid) (cloudops_requests_per_second{{{s}}})",
            ),
            "error_ratio": (
                "ratio",
                "sum by(namespace,deployment_uid) (cloudops_error_ratio{{{s}}})",
            ),
            "p95_latency": (
                "seconds",
                "sum by(namespace,deployment_uid) (cloudops_p95_latency_seconds{{{s}}})",
            ),
            "requests": (
                "count",
                "lab_http_request_duration_seconds_count{{{s}}} and (time() - "
                "timestamp(lab_http_request_duration_seconds_count{{{s}}}) < 10)",
            ),
            "sample_time": (
                "timestamp",
                "timestamp(lab_http_request_duration_seconds_count{{{s}}}) and (time() - "
                "timestamp(lab_http_request_duration_seconds_count{{{s}}}) < 10)",
            ),
            "resets": (
                "count",
                "resets(lab_http_request_duration_seconds_count{{{s}}}[30s]) and (time() - "
                "timestamp(lab_http_request_duration_seconds_count{{{s}}}) < 10)",
            ),
            "offered_rate": (
                "requests/s",
                "sum by(namespace,deployment_uid) (rate(lab_offered_requests_total{{{s}}}[30s]))",
            ),
            "offered_sample_time": (
                "timestamp",
                "max by(namespace,deployment_uid) (timestamp(lab_offered_requests_total{{{s}}}))",
            ),
        }
        selector = 'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"'
        plans = {
            "recovery-traffic-scale": ("scale_deployment", [2], 180, 25.0),
            "recovery-oom-memory": ("set_memory_profile", [1], 300, 7.0),
            "recovery-bad-rollout-restore": ("restore_deployment_template", [1], 300, 7.0),
        }
        profiles = [
            {
                "schema_version": "RecoveryVerificationProfile/v1",
                "id": RECOVERY_POLICY_ID,
                "version": f"{scenario}/{RECOVERY_SOURCE_VERSION[:16]}",
                "workload_uid": uid,
                "template": template,
                "containers": "SELECTED",
                "image_ids": {"app": image_id},
                "replicas": replicas,
                "source_version": RECOVERY_SOURCE_VERSION,
                "source_digest": "SOURCE",
                "qualification_reference": f"local://e10-s3/qualification/{scenario}.json",
                "window_seconds": window,
                "minimum_coverage": 0.9,
                "minimum_rps": minimum,
                "minimum_load_ratio": 0.9,
                "maximum_error_ratio": 0.05,
                "maximum_p95_seconds": 0.5,
                "queries": {
                    key: {"query": query.format(s=selector), "unit": unit}
                    for key, (unit, query) in queries.items()
                },
            }
            for scenario, (template, replicas, window, minimum) in plans.items()
        ]
        policy = {
            "schema_version": "RecoveryPolicy/v1",
            "id": "e10-s3-recovery",
            "version": RECOVERY_SOURCE_VERSION[:16],
            "workload_uids": [uid],
            "max_cpu": "500m",
            "max_memory": "512Mi",
            "qualified_images": [
                RECOVERY_IMAGES["healthy"],
                self.recovery_healthy_fields()["image"],
            ],
            "memory_profiles": [
                {
                    "id": "e10-s3-recovery-memory",
                    "version": RECOVERY_SOURCE_VERSION[:16],
                    "workload_uid": uid,
                    "containers": ["app"],
                    "request": RECOVERY_RESOURCES["requests"]["memory"],
                    "limit": RECOVERY_RESOURCES["limits"]["memory"],
                    "qualification_reference": "local://e10-s3/qualification/recovery-oom-memory.json",
                }
            ],
            "verification_policy": RECOVERY_POLICY_ID,
            "inverse_policy": "unavailable/v1",
        }
        # The API process derives the exact selected containers, source digest and
        # canonical digests; the manager never re-implements those contracts.
        script = """
import json, sys
from cloudops.features.recovery.fields import selected
from cloudops.features.recovery.policy import RecoveryPolicy
from cloudops.features.recovery.verification_policy import VerificationProfile, source_digest
value = json.load(sys.stdin)
containers, _ = selected(value["deployment"])
source = source_digest()
profiles = []
for raw in value["profiles"]:
    raw["containers"] = containers
    raw["source_digest"] = source
    profiles.append(VerificationProfile.model_validate(raw))
policy = RecoveryPolicy.model_validate(value["policy"])
print(json.dumps({
    "policy": policy.model_dump(mode="json"),
    "policy_digest": policy.digest(),
    "profiles": [p.model_dump(mode="json") for p in profiles],
    "profile_digests": [p.digest() for p in profiles],
    "source_digest": source,
}))
"""
        result = json.loads(
            self.dc(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                script,
                body=json.dumps({"deployment": deployment, "profiles": profiles, "policy": policy}),
            )
            .strip()
            .splitlines()[-1]
        )
        folder = self.directory / "recovery"
        folder.mkdir(mode=0o700, exist_ok=True)
        private_write(folder / "policy.json", json.dumps(result["policy"], indent=2) + "\n")
        private_write(folder / "verification.json", json.dumps(result["profiles"], indent=2) + "\n")
        digests = {
            scenario: digest
            for scenario, digest in zip(plans, result["profile_digests"], strict=True)
        }
        private_write(
            folder / "profiles.json",
            json.dumps({"target_uid": uid, "image_id": image_id, "digests": digests}, indent=2)
            + "\n",
        )
        published = json.loads(
            self.dc(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import json;from cloudops.features.recovery.verification_policy import "
                "configured_profiles;print(json.dumps([p.digest() for p in configured_profiles()]))",
            )
            .strip()
            .splitlines()[-1]
        )
        require(
            sorted(published) == sorted(digests.values()),
            "The API does not load exactly the generated qualified profiles.",
        )
        self.receipt(
            "recovery-config",
            {
                "target_uid": uid,
                "image_id": image_id,
                "policy_digest": result["policy_digest"],
                "profile_digests": digests,
                "source_digest": result["source_digest"],
                "source_version": RECOVERY_SOURCE_VERSION,
                "loaded_by_api": True,
            },
        )

    def qualified_profile(self, scenario_id):
        folder = self.directory / "recovery"
        profiles = json.loads((folder / "verification.json").read_text())
        digests = json.loads((folder / "profiles.json").read_text())["digests"]
        template = {
            "recovery-traffic-scale": "scale_deployment",
            "recovery-oom-memory": "set_memory_profile",
            "recovery-bad-rollout-restore": "restore_deployment_template",
        }[scenario_id]
        matches = [profile for profile in profiles if profile["template"] == template]
        require(len(matches) == 1, "Exactly one generated profile must match the scenario.")
        return matches[0], digests[scenario_id]

    def profile_sample(self, profile, uid):
        values = {}
        for key in ("request_rate", "error_ratio", "p95_latency", "offered_rate"):
            query = (
                profile["queries"][key]["query"]
                .replace("{{namespace}}", "demo")
                .replace("{{workload_uid}}", uid)
            )
            rows = self.prom("/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
            values[key] = float(rows[0]["value"][1]) if len(rows) == 1 else None
        return values

    def qualify(self, scenario_id, rate, seconds):
        """Measure the exact post-action state under the fixed offered load before any trial.

        This is a trusted operator calibration of the profile's desired state, not a product
        action and not credit for a recovery. The baseline is restored before returning.
        """
        self.require_idle_recovery_lab()
        require(scenario_id in RECOVERY_FAULTS, "Unknown recovery scenario.")
        require(1 <= rate <= 30 and 120 <= seconds <= 900, "Qualification load bounds exceeded.")
        profile, profile_digest = self.qualified_profile(scenario_id)
        deployment = self.deployment("checkout")
        baseline = owned_fields(deployment)
        require(
            baseline == self.recovery_healthy_fields()
            and int(deployment["spec"].get("replicas", 1)) == 1,
            "Qualification starts only from the captured healthy recovery baseline.",
        )
        health = self.healthy_target(deployment)
        uid = health["target_uid"]
        template = copy.deepcopy(baseline)
        template["resources"] = profile["containers"][0]["resources"]
        action_state = {
            "replicas": profile["replicas"][0],
            "template": template,
            "rolling_update": deployment["spec"].get("strategy", {}).get("rollingUpdate", {}),
        }
        self.validate_qualified_recovery_action(scenario_id, deployment, action_state)
        proof = self.containment(read_only=True)
        source = self.source_binding()
        changes = []
        if action_state["replicas"] != 1:
            changes.append({"op": "replace", "path": "/spec/replicas", "value": 2})
        if template != baseline:
            changes += field_changes(deployment, template)
        samples = []
        container_id = None
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            if changes:
                self.patch(deployment, changes)
            self.wait_settled()
            container_id, container = self.start_offered_load(
                f"cloudops-e10s3-q-{uuid4().hex[:8]}", seconds, rate, uid
            )
            require(container["State"]["Running"], "Qualification load did not start.")
            time.sleep(profile["warmup_seconds"])
            slots = (seconds - profile["warmup_seconds"] - 15) // profile["step_seconds"]
            for _ in range(max(1, slots)):
                samples.append(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        **self.profile_sample(profile, uid),
                    }
                )
                time.sleep(profile["step_seconds"])
        finally:
            if container_id:
                self.command(["docker", "stop", "--time", "3", container_id], timeout=20)
                self.command(["docker", "rm", container_id], timeout=20)
            live = self.deployment("checkout")
            restore = []
            if int(live["spec"].get("replicas", 1)) != 1:
                restore.append({"op": "replace", "path": "/spec/replicas", "value": 1})
            if owned_fields(live) != baseline:
                restore += field_changes(live, baseline)
            if restore:
                self.patch(live, restore)
            self.wait_settled()
        restored = self.deployment("checkout")
        require(
            owned_fields(restored) == baseline
            and int(restored["spec"].get("replicas", 1)) == 1
            and restored["metadata"]["uid"] == uid,
            "Qualification did not restore the captured baseline.",
        )

        def passing(sample):
            values = [sample[key] for key in sample if key != "at"]
            return (
                all(value is not None and math.isfinite(value) for value in values)
                and sample["request_rate"] >= profile["minimum_rps"]
                and sample["offered_rate"] >= rate * 0.8
                and sample["request_rate"] >= sample["offered_rate"] * profile["minimum_load_ratio"]
                and sample["error_ratio"] <= profile["maximum_error_ratio"]
                and sample["p95_latency"] <= profile["maximum_p95_seconds"]
            )

        passed = bool(samples) and all(passing(sample) for sample in samples)
        demand = self.recovery_surge_demand(deployment, action_state)
        calibration = {
            "schema": "E10RecoveryCalibration/v1",
            "scenario_id": scenario_id,
            "target_uid": uid,
            "profile_digest": profile_digest,
            "rate_rps": rate,
            "seconds": seconds,
            "started_at": started_at,
            "action_state": action_state,
            "samples": samples,
            "passed": passed,
            "containment": proof,
            "note": "Operator calibration of the desired post-action state; not a recovery result.",
        }
        folder = self.directory / "qualification"
        folder.mkdir(mode=0o700, exist_ok=True)
        private_write(
            folder / f"{scenario_id}-calibration-{time.time_ns()}.json",
            json.dumps(calibration, indent=2) + "\n",
        )
        self.receipt("recovery-calibration", calibration)
        require(passed, "The desired state did not meet its profile; the target stays manual.")
        qualification = validate_qualification_receipt(
            {
                "schema": "E10RecoveryQualificationReceipt/v1",
                "scenario_id": scenario_id,
                "qualified": True,
                "target_uid": uid,
                "source_binding_sha256": source["sha256"],
                "profile_digest": profile_digest,
                "image_digests": health["image_digests"],
                "declared_resources": action_state,
                "surge_headroom": demand,
            }
        )
        private_write(folder / f"{scenario_id}.json", json.dumps(qualification, indent=2) + "\n")
        print(json.dumps({"qualification": str(folder / f"{scenario_id}.json")}))

    def writer(self, operation):
        """Open or close the isolated writer around one explicitly approved product action."""
        require(RECOVERY_LAB, "Writer lifecycle commands belong to the E10 S3 recovery lab.")
        if operation == "enable":
            require(self.trial_path.is_file(), "Enable the writer only for an active trial.")
            trial = json.loads(self.trial_path.read_text())
            require(
                trial["scenario_kind"] == "recovery" and trial["status"] == "observed",
                "Enable the writer only after a qualified recovery fault is observed.",
            )
            permit = json.loads(
                self.dc("run", "--rm", "-T", "--no-deps", "dispatch-admin", "status")
            )["permit"]
            if permit["mode"] == "recovery":
                self.dispatch("resume", "--class", "recovery_execution")
            else:
                require(
                    "recovery_execution" in permit.get("allowed_job_kinds", []),
                    "Recovery execution dispatch is not permitted.",
                )
            results = {
                operation_name: self.maintenance(operation_name).get("status")
                for operation_name in ("enable_target", "exit_recovery_mode")
            }
        else:
            self.dispatch("enter-recovery")
            results = {"enter_recovery": self.maintenance("enter_recovery").get("status")}
            for kind in ("collection", "recommendation"):
                self.dispatch("resume", "--class", kind)
        status = self.maintenance()
        self.receipt("writer-" + operation, {"results": results, "targets": status["targets"]})
        if operation == "enable":
            require(set(results.values()) == {"applied"}, "Writer enablement was not applied.")
        else:
            require(
                all(
                    not target["enabled"] and target["recovery_mode"]
                    for target in status["targets"]
                ),
                "Writer did not return to quarantine.",
            )

    def load(self, target, seconds, rate, *, recovery=False, detach=False):
        if recovery:
            require(
                target == "checkout",
                "Recovery load is fixed to the owned checkout target.",
            )
            require(1 <= seconds <= 900, "Recovery load is bounded to 1..900 seconds.")
            require(
                detach,
                "Recovery load must be detached so the product action can run concurrently.",
            )
            require(
                self.trial_path.is_file(),
                "Begin an E10 recovery trial before its load.",
            )
            trial = json.loads(self.trial_path.read_text())
            require(
                trial["scenario_kind"] == "recovery" and trial["status"] == "ready",
                "Recovery load requires a ready qualified recovery trial.",
            )
            require(
                trial.get("load") is None,
                "This trial already has an offered-load record.",
            )
            require(not self.active_loads(), "A targeted load is already running.")
            self.no_execution()
            require(
                self.eligible_action_count() == 0,
                "An unexpired eligible action exists.",
            )
            proof = self.containment(read_only=True)
            container_id, container = self.start_offered_load(
                f"cloudops-e10s3-{trial['trial_id'][:12]}", seconds, rate, trial["target_uid"]
            )
            running = container.get("State", {}).get("Running") is True
            trial["load"] = {
                "container_id": container_id,
                "seconds": seconds,
                "rate": rate,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "state": "running" if running else "failed",
            }
            if not running:
                trial["load"]["exit_code"] = container.get("State", {}).get("ExitCode")
                trial["load"]["diagnostic_tail"] = self.command(
                    ["docker", "logs", "--tail", "5", container_id], timeout=15
                )
                self.command(["docker", "rm", container_id], timeout=15)
            private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
            self.receipt(
                "trial-load-start",
                {
                    "trial_id": trial["trial_id"],
                    "load": trial["load"],
                    "containment": proof,
                },
            )
            require(
                running,
                "Recovery load exited before it started; inspect its private receipt.",
            )
            return

        require(not detach, "Detached load is reserved for a qualified E10 recovery trial.")
        if self.trial_path.is_file():
            require(
                json.loads(self.trial_path.read_text()).get("scenario_kind") != "recovery",
                "A recovery trial owns the target's offered load; no other targeted load may run.",
            )
        require(not self.active_loads(), "A targeted load is already running.")
        proof = self.containment(read_only=True)
        result = json.loads(
            self.dc(
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "load",
                "--target",
                target,
                "--seconds",
                str(seconds),
                "--rate",
                str(rate),
                timeout=seconds + 30,
            )
        )
        self.receipt("load", {"containment": proof, **result})

    def finish_recovery_load(self, *, interrupt=False):
        require(self.trial_path.is_file(), "No active E10 trial has a recovery load.")
        trial = json.loads(self.trial_path.read_text())
        load = trial.get("load")
        require(isinstance(load, dict), "The active trial has no recovery load record.")
        require(load.get("state") == "running", "The recovery load was already finalized.")
        container_id = load["container_id"]
        container = json.loads(self.command(["docker", "inspect", container_id]))[0]
        labels = container.get("Config", {}).get("Labels", {})
        require(
            labels.get(OWNER_LABEL) == self.marker["owner"]
            and labels.get("com.docker.compose.project") == OWNED_PROJECT
            and labels.get("com.docker.compose.service") == "load"
            and labels.get("com.docker.compose.project.working_dir") == str(ROOT),
            "Recovery load container is outside the owned project.",
        )
        was_running = container.get("State", {}).get("Running") is True
        if was_running:
            require(
                interrupt,
                "Recovery load is still running; let its fixed window finish first.",
            )
            self.command(["docker", "stop", "--time", "3", container_id], timeout=15)
        container = json.loads(self.command(["docker", "inspect", container_id]))[0]
        exit_code = container.get("State", {}).get("ExitCode")
        logs = self.command(["docker", "logs", "--tail", "5", container_id], timeout=15)
        result = None
        for line in reversed(logs.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "completed_nominal_load" in candidate:
                result = candidate
                break
        finished_at = datetime.now(timezone.utc).isoformat()
        if interrupt and was_running and trial.get("post_check", {}).get("status") == "passed":
            state = "stopped_after_post_check"
        elif interrupt and was_running:
            state = "interrupted"
        elif exit_code == 0 and result and result.get("completed_nominal_load") is True:
            state = "complete"
        else:
            state = "failed"
        load.update(
            {
                "state": state,
                "finished_at": finished_at,
                "exit_code": exit_code,
                "result": result,
            }
        )
        if trial.get("post_check", {}).get("status") == "passed":
            trial["post_check"]["load_stopped_at"] = finished_at
            trial["post_check"]["load_final_state"] = state
        self.command(["docker", "rm", container_id], timeout=15)
        private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
        self.receipt(
            "trial-load-stop",
            {
                "trial_id": trial["trial_id"],
                "load": load,
                "interrupted": state == "interrupted",
            },
        )

    def recovery_post_check(self, execution_path, verification_path):
        require(self.trial_path.is_file(), "No active E10 recovery trial exists.")
        trial = json.loads(self.trial_path.read_text())
        require(
            trial.get("scenario_kind") == "recovery"
            and trial.get("status") == "observed"
            and trial.get("qualification") is not None,
            "A qualified, observed recovery trial is required before positive verification.",
        )
        require(
            trial.get("post_check") is None,
            "This trial already has a positive post-check attestation.",
        )
        fault_path = self.directory / "fault.json"
        require(fault_path.is_file(), "The bound observed fault is missing.")
        fault = json.loads(fault_path.read_text())
        require(
            fault.get("trial_id") == trial["trial_id"]
            and fault.get("run_id") == trial["run_id"]
            and fault.get("window_id") == trial["window_id"]
            and fault.get("source_binding_sha256") == trial["source_binding_sha256"]
            and fault.get("uid") == trial["target_uid"]
            and fault.get("observation_sha256") == trial.get("observation_sha256")
            and fault.get("observed_at"),
            "Observed fault identity does not match the active trial.",
        )
        load = trial.get("load") or {}
        active = self.active_loads()
        require(
            load.get("state") == "running"
            and len(active) == 1
            and active[0]["Id"] == load.get("container_id"),
            "The exact bounded offered load must remain active through positive verification.",
        )
        control = read_json(ROOT / load_catalog()["source_fixtures"]["e10_s3_controls"]["path"])[
            "controls"
        ][trial["scenario_id"]]
        require(
            fault.get("scenario") == control.get("local_fault_operation"),
            "Observed fault treatment changed after injection.",
        )
        execution_file = Path(execution_path)
        verification_file = Path(verification_path)
        require(
            execution_file.is_file()
            and not execution_file.is_symlink()
            and verification_file.is_file()
            and not verification_file.is_symlink(),
            "Use regular private JSON files captured from the E07 read endpoints.",
        )
        execution_file = execution_file.resolve(strict=True)
        verification_file = verification_file.resolve(strict=True)
        qualified = trial["qualification"]
        post_check = validate_recovery_post_check(
            read_json(execution_file),
            read_json(verification_file),
            scenario_id=trial["scenario_id"],
            target_uid=trial["target_uid"],
            profile_digest=qualified["profile_digest"],
            load=load,
            fault_observed_at=fault["observed_at"],
        )
        deployment = self.deployment("checkout")
        action = qualified["declared_resources"]
        require(
            deployment["metadata"]["uid"] == trial["target_uid"]
            and int(deployment["spec"].get("replicas", 1)) == action["replicas"]
            and deployment["spec"].get("strategy", {}).get("rollingUpdate", {})
            == action["rolling_update"]
            and owned_fields(deployment) == action["template"],
            "Post-check target does not match the exact qualified action state.",
        )
        health = self.healthy_target(deployment)
        require(
            health["target_uid"] == trial["target_uid"]
            and health["image_digests"] == trial["image_digests"],
            "Post-check target UID or runtime images changed.",
        )
        verification = self.verify(self.trial_target_uids())
        metrics = self.target_metric_snapshot(verification, trial["target_uid"])
        source = self.source_binding()
        require(
            source["sha256"] == trial["source_binding_sha256"],
            "Post-check source binding changed after trial readiness.",
        )
        offered = self.offered_rate_snapshot(trial["target_uid"], load["rate"])
        observed_at = datetime.now(timezone.utc).isoformat()
        treatment_digest = hashlib.sha256(
            json.dumps(fault["expected"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        action_state = {
            "replicas": action["replicas"],
            "template": owned_fields(deployment),
            "rolling_update": action["rolling_update"],
        }
        trial["post_check"] = {
            **post_check,
            "trial_id": trial["trial_id"],
            "run_id": trial["run_id"],
            "window_id": trial["window_id"],
            "target_uid": trial["target_uid"],
            "execution_receipt_sha256": sha256_file(execution_file),
            "verification_receipt_sha256": sha256_file(verification_file),
            "fault_observation_sha256": fault["observation_sha256"],
            "fault_treatment_sha256": treatment_digest,
            "action_state": action_state,
            "load_started_at": load["started_at"],
            "post_check_observed_at": observed_at,
            "offered_sample_at": datetime.fromtimestamp(
                float(offered["sample_at"]), timezone.utc
            ).isoformat(),
            "requested_rate_rps": load["rate"],
            "observed_rate_rps": offered["observed_rate_rps"],
            "load_stopped_at": None,
            "load_final_state": None,
        }
        trial["load"].setdefault("observations", []).append(
            {"phase": "positive-post-check", **offered}
        )
        trial["phases"]["apply"] = post_check["verification_acknowledged_at"]
        trial["phases"]["verify"] = post_check["verification_finalized_at"]
        trial["status"] = "post_checked"
        private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
        self.receipt(
            "trial-positive-post-check",
            {
                "trial_id": trial["trial_id"],
                "run_id": trial["run_id"],
                "window_id": trial["window_id"],
                "scenario_id": trial["scenario_id"],
                "target_uid": trial["target_uid"],
                "status": "passed",
                "execution_id": post_check["execution_id"],
                "verification_profile_digest": post_check["verification_profile_digest"],
                "verification_finalized_at": post_check["verification_finalized_at"],
                "execution_receipt_sha256": trial["post_check"]["execution_receipt_sha256"],
                "verification_receipt_sha256": trial["post_check"]["verification_receipt_sha256"],
                "fault_observation_sha256": fault["observation_sha256"],
                "fault_treatment_sha256": treatment_digest,
                "action_state_sha256": hashlib.sha256(
                    json.dumps(action_state, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "application_metrics": metrics,
                "offered_load": offered,
                "source_binding_sha256": source["sha256"],
            },
        )

    def fault(self, scenario):
        require(
            not (self.directory / "fault.json").exists(),
            "Reset the recorded fault before another injection.",
        )
        trial = None
        if self.trial_path.exists():
            trial = json.loads(self.trial_path.read_text())
            validate_catalog()
            require(
                sha256_file(ROOT / "config/lab/release-scenarios-v1.json")
                == trial["catalog_sha256"],
                "Scenario catalog changed after readiness; no fault may be injected.",
            )
            require(
                trial.get("status") == "ready",
                "This trial is manual or incomplete; fault injection is refused.",
            )
            controls = read_json(
                ROOT / load_catalog()["source_fixtures"]["e10_s3_controls"]["path"]
            )["controls"]
            control = controls[trial["scenario_id"]]
            require(
                control.get("local_fault_operation") == scenario,
                "The selected fixture fault does not match this catalog scenario.",
            )
        self.no_execution()
        require(
            self.eligible_action_count() == 0,
            "An unexpired eligible action exists; fault injection is refused.",
        )
        proof = self.containment()
        before = self.deployment("checkout")
        fields = owned_fields(before)
        if trial is not None:
            if trial["scenario_kind"] == "recovery":
                load = trial.get("load") or {}
                active = self.active_loads()
                require(
                    load.get("state") == "running"
                    and len(active) == 1
                    and active[0]["Id"] == load.get("container_id"),
                    "Recovery fault requires its exact bounded offered load to be running.",
                )
                current_health = self.healthy_target(before)
                require(
                    current_health["target_uid"] == trial["target_uid"]
                    and current_health["image_digests"] == trial["image_digests"],
                    "Recovery target UID or qualified runtime images changed before injection.",
                )
                current_metrics = self.verify(self.trial_target_uids())
                self.target_metric_snapshot(current_metrics, trial["target_uid"])
                offered = self.offered_rate_snapshot(trial["target_uid"], load["rate"])
                trial["load"].setdefault("observations", []).append(
                    {"phase": "pre-injection", **offered}
                )
            require(
                before["metadata"]["uid"] == trial["target_uid"]
                and fields == trial["baseline"]["owned_fields"]
                and self.source_binding()["sha256"] == trial["source_binding_sha256"],
                "Trial target, fields or source binding changed after readiness.",
            )
        wanted = copy.deepcopy(fields)
        if scenario in set(RECOVERY_FAULTS.values()):
            require(
                RECOVERY_LAB and trial is not None and trial["scenario_kind"] == "recovery",
                "Recovery faults require a ready recovery trial in the E10 S3 recovery lab.",
            )
            if scenario == "memory-limit":
                # A real OOM: the 160 MiB resident working set cannot fit this limit.
                wanted["resources"]["requests"]["memory"] = FAULT_MEMORY_LIMIT
                wanted["resources"]["limits"]["memory"] = FAULT_MEMORY_LIMIT
            elif scenario == "bad-rollout":
                wanted["image"] = "docker.io/library/" + RECOVERY_IMAGES["bad-image"]
            # overload: the fixed offered load alone is the fault; no field changes.
        elif scenario in {"crashloop", "oom", "traffic"}:
            wanted["mode"] = {
                "name": "LAB_MODE",
                "value": {"crashloop": "crash", "oom": "oom", "traffic": "slow"}[scenario],
            }
            if scenario == "oom":
                wanted["resources"]["limits"]["memory"] = "32Mi"
        elif scenario == "pending":
            wanted["resources"]["requests"]["cpu"] = "100"
            wanted["resources"]["limits"]["cpu"] = "100"
        elif scenario == "imagepull":
            wanted["image"] = "127.0.0.1:1/cloudops-unavailable:bounded"
            wanted["imagePullPolicy"] = "Always"
        elif scenario == "init-retry":
            require(
                wanted["initContainers"] is None,
                "Existing init containers cannot be replaced by this fixture.",
            )
            wanted["initContainers"] = [
                {
                    "name": "setup",
                    "image": SAMPLE_CTR_IMAGE,
                    "imagePullPolicy": "Never",
                    "command": ["python", "/app/sample_app.py", "--init"],
                    "terminationMessagePath": "/dev/termination-log",
                    "terminationMessagePolicy": "File",
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "32Mi"},
                    },
                }
            ]
        else:
            raise RuntimeError("Unknown fixed fault.")
        record = {
            "uid": before["metadata"]["uid"],
            "scenario": scenario,
            "before": fields,
            "expected": wanted,
            "at": datetime.now(timezone.utc).isoformat(),
            "state": "prepared",
        }
        if trial is not None:
            record.update(
                {
                    "trial_id": trial["trial_id"],
                    "run_id": trial["run_id"],
                    "window_id": trial["window_id"],
                    "scenario_id": trial["scenario_id"],
                    "source_binding_sha256": trial["source_binding_sha256"],
                }
            )
        private_write(self.directory / "fault.json", json.dumps(record))
        if wanted != fields:
            self.patch(before, field_changes(before, wanted))
        record["state"] = "injected"
        private_write(self.directory / "fault.json", json.dumps(record))
        if trial is not None:
            trial["status"] = "injected"
            trial["phases"]["inject"] = record["at"]
            private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
        self.receipt(
            "fault",
            {
                "target_uid": record["uid"],
                "trial_id": record.get("trial_id"),
                "run_id": record.get("run_id"),
                "window_id": record.get("window_id"),
                "scenario_id": record.get("scenario_id"),
                "injected_at": record["at"],
                "containment": proof,
                "observation": "Run observe to require actual runtime evidence; injection is not a pass.",
            },
        )

    def incomplete_trial_reset(self, trial, reason, *, interrupted=False):
        if trial is None:
            return
        try:
            residual_actions = self.eligible_action_count()
        except (RuntimeError, OSError, ValueError):
            residual_actions = None
        try:
            duplicate_loads = len(self.active_loads())
        except (RuntimeError, OSError, ValueError):
            duplicate_loads = None
        reset_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": "E10TrialResetReceipt/v1",
            "trial_id": trial["trial_id"],
            "run_id": trial["run_id"],
            "window_id": trial["window_id"],
            "reset_id": str(uuid4()),
            "scenario_id": trial["scenario_id"],
            "reset_state": "interrupted" if interrupted else "faulted",
            "scope": trial["scope"],
            "target_uid": trial["target_uid"],
            "source_binding_sha256": trial["source_binding_sha256"],
            "image_digests": trial["image_digests"],
            "declared_resources": trial["declared_resources"],
            "surge_headroom": trial["surge_headroom"],
            "baseline_at": trial["baseline_at"],
            "observed_at": reset_at,
            "source_limits": trial["source_limits"],
            "residual_eligible_actions": residual_actions,
            "duplicate_targeted_loads": duplicate_loads,
            "receipt_status": "incomplete",
            "incomplete_reason": reason,
            "phases": trial["phases"],
            "evidence": {
                "reset_attempt": "refused before completion",
                "offered_load": trial.get("load"),
            },
        }
        validate_trial_receipt(payload)
        private_write(
            self.directory / "receipts" / f"{time.time_ns()}-trial-reset-incomplete.json",
            json.dumps(payload, indent=2) + "\n",
        )
        self.receipt("trial-reset", {"trial_receipt": payload})

    def reset(self, interrupted=False):
        trial = json.loads(self.trial_path.read_text()) if self.trial_path.is_file() else None
        if (
            trial
            and trial["scenario_kind"] == "recovery"
            and trial.get("load")
            and trial["load"].get("state") == "running"
        ):
            self.incomplete_trial_reset(
                trial,
                "Recovery offered load must be finalized before reset.",
                interrupted=interrupted,
            )
            require(
                False,
                "Recovery offered load must finish or be explicitly stopped before reset.",
            )
        try:
            self.no_execution()
        except RuntimeError as error:
            self.incomplete_trial_reset(trial, str(error), interrupted=interrupted)
            raise
        try:
            residual_before = self.eligible_action_count()
            loads_before = self.active_loads()
        except (RuntimeError, OSError, ValueError) as error:
            self.incomplete_trial_reset(trial, str(error), interrupted=interrupted)
            raise
        if residual_before or loads_before:
            reason = (
                "Eligible action remains." if residual_before else "Targeted load is still running."
            )
            self.incomplete_trial_reset(trial, reason, interrupted=interrupted)
            require(False, reason + " Reset refused.")
        fault_path = self.directory / "fault.json"
        has_fault = fault_path.is_file()
        require(
            has_fault or trial is not None,
            "No recorded fault or E10 trial; refusing an unspecified reset.",
        )
        record = json.loads(fault_path.read_text()) if has_fault else None
        try:
            live = self.deployment("checkout")
        except RuntimeError as error:
            self.incomplete_trial_reset(trial, str(error), interrupted=interrupted)
            raise
        current = owned_fields(live)

        if has_fault:
            allowed_fault_fields = [record["expected"], record["before"]]
            if trial and trial["scenario_kind"] == "recovery":
                recovered = copy.deepcopy(record["before"])
                recovered["mode"] = record["expected"]["mode"]
                allowed_fault_fields.append(recovered)
                if trial.get("qualification"):
                    allowed_fault_fields.append(
                        trial["qualification"]["declared_resources"]["template"]
                    )
            if trial and (
                current not in allowed_fault_fields
                or live["metadata"]["uid"] != record["uid"]
                or not self.trial_rollout_parameters_are_allowed(trial, live)
            ):
                self.incomplete_trial_reset(
                    trial,
                    "Target UID, template, replicas or rollout strategy changed after injection; restore the exact trial baseline before reset.",
                    interrupted=interrupted,
                )
            require(
                current in allowed_fault_fields,
                "Owned fields changed after injection; manual inspection required.",
            )
            require(
                live["metadata"]["uid"] == record["uid"],
                "Fault target was replaced; reset refused.",
            )
            if trial:
                require(
                    self.trial_rollout_parameters_are_allowed(trial, live),
                    "Trial replicas or rollout strategy do not match the recorded baseline or qualified action state.",
                )
                require(
                    record.get("trial_id") == trial["trial_id"]
                    and record.get("run_id") == trial["run_id"]
                    and record.get("window_id") == trial["window_id"]
                    and record.get("source_binding_sha256") == trial["source_binding_sha256"],
                    "Fault record does not match its trial bindings.",
                )
            baseline_fields = record["before"]
            changes = field_changes(live, baseline_fields) if current != baseline_fields else []
            if trial and live["spec"].get("replicas", 1) != trial["declared_resources"]["replicas"]:
                changes.append(
                    {
                        "op": "replace",
                        "path": "/spec/replicas",
                        "value": trial["declared_resources"]["replicas"],
                    }
                )
            if changes:
                self.patch(live, changes)
            self.wait_settled()
        else:
            if trial and (
                trial["status"] not in {"ready", "manual"}
                or live["metadata"]["uid"] != trial["target_uid"]
                or current != trial["baseline"]["owned_fields"]
                or not self.trial_spec_matches_baseline(trial, live)
            ):
                self.incomplete_trial_reset(
                    trial,
                    "Healthy target no longer matches the recorded trial baseline.",
                    interrupted=interrupted,
                )
            require(
                trial["status"] in {"ready", "manual"}
                and live["metadata"]["uid"] == trial["target_uid"]
                and current == trial["baseline"]["owned_fields"],
                "Healthy reset did not start from its recorded baseline; manual inspection required.",
            )
            require(
                self.trial_spec_matches_baseline(trial, live),
                "Healthy reset replicas or rollout strategy differ from the trial baseline.",
            )
            baseline_fields = trial["baseline"]["owned_fields"]

        reset_at = datetime.now(timezone.utc).isoformat()
        try:
            readiness = self.readiness_load()
            verification = self.verify(self.trial_target_uids())
            containment = self.containment(read_only=True)
            live = self.deployment("checkout")
            require(
                live["metadata"]["uid"] == (trial["target_uid"] if trial else record["uid"])
                and owned_fields(live) == baseline_fields,
                "Reset target UID or owned fields changed before completion; keep the receipt state.",
            )
            health = self.healthy_target(live)
            metrics = self.target_metric_snapshot(verification, health["target_uid"])
            source = self.source_binding()
            if trial:
                require(
                    health["target_uid"] == trial["target_uid"]
                    and health["image_digests"] == trial["image_digests"]
                    and source["sha256"] == trial["source_binding_sha256"]
                    and source["scope_manifest_sha256"] == trial["scope"]["scope_manifest_sha256"]
                    and self.trial_spec_matches_baseline(trial, live),
                    "Reset lost the exact target, runtime image, source or declared resource qualification.",
                )
            if trial and trial.get("qualification"):
                headroom, _demand = self.recovery_surge_headroom(
                    live, trial["qualification"]["declared_resources"]
                )
            else:
                headroom = self.surge_headroom(live)
            active_loads = self.active_loads()
            residual_actions = self.eligible_action_count()
        except (RuntimeError, OSError, ValueError) as error:
            self.incomplete_trial_reset(
                trial,
                str(error),
                interrupted=interrupted,
            )
            raise
        receipt_status = "reset_complete"
        reasons = []
        if active_loads:
            receipt_status = "incomplete"
            reasons.append("A targeted load reappeared during reset verification.")
        if residual_actions:
            receipt_status = "incomplete"
            reasons.append("An unexpired eligible action remains after reset.")
        if not headroom["qualified"]:
            reason = "Measured post-reset surge headroom is below the owned target demand."
            reasons.append(reason)
            if not trial or trial["status"] != "manual":
                receipt_status = "incomplete"
        if trial and source["sha256"] != trial["source_binding_sha256"]:
            receipt_status = "incomplete"
            reasons.append("Source binding changed after trial readiness.")
        if (
            trial
            and sha256_file(ROOT / "config/lab/release-scenarios-v1.json")
            != trial["catalog_sha256"]
        ):
            receipt_status = "incomplete"
            reasons.append("Scenario catalog changed after trial readiness.")
        if trial and trial["status"] == "manual":
            reasons.append(trial["manual_reason"])
            if receipt_status != "incomplete":
                receipt_status = "manual"
        # The baseline itself was restored and verified. A recovery trial without a positive
        # post-check stays an incomplete trial, but it must not wedge the next fresh trial.
        lab_restored = receipt_status != "incomplete"
        if trial and trial["scenario_kind"] == "recovery" and receipt_status == "reset_complete":
            post_check = trial.get("post_check") or {}
            load = trial.get("load") or {}
            if (
                post_check.get("status") != "passed"
                or load.get("state") not in {"complete", "stopped_after_post_check"}
                or not post_check.get("load_stopped_at")
                or post_check.get("load_final_state") != load.get("state")
                or post_check.get("load_stopped_at") != load.get("finished_at")
            ):
                receipt_status = "incomplete"
                reasons.append(
                    "Recovery reset lacks a positive E07 post-check and finalized offered-load evidence."
                )

        reset_id = str(uuid4())
        scenario_id = trial["scenario_id"] if trial else record.get("scenario_id")
        if trial:
            trial["reset_id"] = reset_id
            trial["observed_at"] = reset_at
            trial["phases"]["reset"] = reset_at
            if trial["scenario_kind"] != "recovery":
                trial["phases"]["verify"] = reset_at
        reset_state = "interrupted" if interrupted else "faulted" if has_fault else "healthy"
        if trial:
            trial_receipt = {
                "schema": "E10TrialResetReceipt/v1",
                "trial_id": trial["trial_id"],
                "run_id": trial["run_id"],
                "window_id": trial["window_id"],
                "reset_id": reset_id,
                "scenario_id": scenario_id,
                "reset_state": reset_state,
                "scope": trial["scope"],
                "target_uid": health["target_uid"],
                "source_binding_sha256": source["sha256"],
                "image_digests": health["image_digests"],
                "declared_resources": trial["declared_resources"],
                "surge_headroom": headroom,
                "baseline_at": trial["baseline_at"],
                "observed_at": reset_at,
                "source_limits": trial["source_limits"],
                "residual_eligible_actions": residual_actions,
                "duplicate_targeted_loads": len(active_loads),
                "receipt_status": receipt_status,
                "incomplete_reason": "; ".join(reasons) or None,
                "phases": trial["phases"],
                "evidence": {
                    "baseline_pod_uids": trial["baseline"]["pod_uids"],
                    "reset_pod_uids": health["pod_uids"],
                    "baseline_owned_fields": trial["baseline"]["owned_fields"],
                    "reset_owned_fields": owned_fields(live),
                    "readiness_load": readiness,
                    "lab_state": "restored_baseline" if lab_restored else "retained_for_inspection",
                    "metrics": metrics,
                    "source_files": source["files"],
                    "containment": containment,
                    "fault": record,
                    "fault_observed": bool(record and record.get("observed_at")),
                    "fault_observed_at": record.get("observed_at") if record else None,
                    "fault_observation_sha256": (
                        record.get("observation_sha256") if record else None
                    ),
                    "offered_load": trial.get("load"),
                    "trial_disposition": (
                        "observed"
                        if record and record.get("observation_sha256")
                        else "healthy_reset"
                        if not record
                        else "incomplete_observation"
                    ),
                },
            }
            if trial.get("post_check") is not None:
                trial_receipt["post_check"] = trial["post_check"]
            validate_trial_receipt(trial_receipt)
            private_write(
                self.directory / "receipts" / f"{time.time_ns()}-trial-reset-private.json",
                json.dumps(trial_receipt, indent=2) + "\n",
            )
            self.receipt("trial-reset", {"trial_receipt": trial_receipt})
            if lab_restored:
                self.trial_path.unlink(missing_ok=True)
                fault_path.unlink(missing_ok=True)
        else:
            private_write(
                self.directory / "receipts" / f"{time.time_ns()}-reset-private.json",
                json.dumps(record, indent=2) + "\n",
            )
            self.receipt(
                "reset",
                {
                    "target_uid": record["uid"],
                    "restored_owned_fields": True,
                    "history": "preserved",
                    "writer": "still quarantined",
                },
            )
            fault_path.unlink()

    def observe(self):
        record = json.loads((self.directory / "fault.json").read_text())
        trial = None
        if self.trial_path.is_file():
            trial = json.loads(self.trial_path.read_text())
            if trial["scenario_kind"] == "recovery":
                load = trial.get("load") or {}
                active = self.active_loads()
                require(
                    load.get("state") == "running"
                    and len(active) == 1
                    and active[0]["Id"] == load.get("container_id"),
                    "Recovery observation requires the exact offered load to remain running.",
                )
        require(
            self.deployment("checkout")["metadata"]["uid"] == record["uid"],
            "Fault record target changed; refusing observation.",
        )

        def observed():
            pods = json.loads(
                self.kube("get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json")
            )["items"]
            for pod in pods:
                self.pod_binding(pod)
                statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get(
                    "status", {}
                ).get("initContainerStatuses", [])
                for status in statuses:
                    waiting = status.get("state", {}).get("waiting", {}).get("reason")
                    terminated = status.get("lastState", {}).get("terminated", {}).get("reason")
                    if record["scenario"] in {"oom", "memory-limit"} and terminated == "OOMKilled":
                        return pod
                    if (
                        record["scenario"] in {"crashloop", "init-retry", "bad-rollout"}
                        and status.get("restartCount", 0) > 0
                    ):
                        retries = json.loads(
                            self.kube(
                                "get",
                                "events",
                                "-n",
                                "demo",
                                "--field-selector",
                                f"involvedObject.uid={pod['metadata']['uid']}",
                                "-o",
                                "json",
                            )
                        )["items"]
                        if retry_failure(status, retries):
                            return pod
                    if record["scenario"] == "imagepull" and waiting in {
                        "ErrImagePull",
                        "ImagePullBackOff",
                    }:
                        return pod
                if record["scenario"] == "pending" and any(
                    c.get("reason") == "Unschedulable"
                    for c in pod.get("status", {}).get("conditions", [])
                ):
                    return pod
            if record["scenario"] in {"traffic", "overload"}:
                query = 'cloudops_error_ratio{deployment_uid="' + record["uid"] + '"}>0.1'
                if self.prom("/api/v1/query?" + urlencode({"query": query}))["data"]["result"]:
                    return {
                        "metadata": {"uid": record["uid"]},
                        "status": {"measured_error_ratio": True},
                    }
            return None

        result = []

        def capture():
            value = observed()
            if value:
                result.append(value)
            return value

        self.wait(capture, "actual fault evidence")
        pod = result[-1]
        events = json.loads(
            self.kube(
                "get",
                "events",
                "-n",
                "demo",
                "--field-selector",
                f"involvedObject.uid={pod['metadata']['uid']}",
                "-o",
                "json",
            )
        )["items"]
        if record["scenario"] in {"pending", "imagepull"}:
            require(events, "Runtime event evidence is absent.")
        logs = []
        if record["scenario"] in {"oom", "crashloop", "init-retry", "memory-limit", "bad-rollout"}:
            container = "setup" if record["scenario"] == "init-retry" else "app"
            for previous in (False, True):
                args = [
                    "logs",
                    pod["metadata"]["name"],
                    "-n",
                    "demo",
                    "-c",
                    container,
                    "--timestamps",
                    "--tail=200",
                    "--limit-bytes=32768",
                    "--since-time=" + record["at"],
                ]
                if previous:
                    args.append("--previous")
                text = self.kube(*args)
                logs.append(
                    {
                        "pod_uid": pod["metadata"]["uid"],
                        "container": container,
                        "previous": previous,
                        "bytes": len(text.encode()),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "timestamped_nonempty": timestamped_nonempty(text),
                        "limit_reached": len(text.encode()) >= 32768
                        or len(text.splitlines()) >= 200,
                        "since_time": record["at"],
                    }
                )
                require(
                    self.get("pod", pod["metadata"]["name"], "demo")["metadata"]["uid"]
                    == pod["metadata"]["uid"],
                    "Pod changed during log read.",
                )
        observation = {
            "target_uid": record["uid"],
            "status": pod["status"],
            "events": [
                {"reason": event.get("reason"), "uid": event["metadata"]["uid"]} for event in events
            ],
            "logs": logs,
            "raw_logs_timestamped_nonempty": all(log["timestamped_nonempty"] for log in logs)
            if logs
            else None,
            "remaining": "Compare stored collector sources with these exact Pod/container/window/options; no completeness waiver.",
        }
        observed_at = datetime.now(timezone.utc).isoformat()
        record["observed_at"] = observed_at
        record["observation_sha256"] = hashlib.sha256(
            json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        private_write(self.directory / "fault.json", json.dumps(record, indent=2) + "\n")
        if self.trial_path.is_file():
            trial = json.loads(self.trial_path.read_text())
            require(
                trial["trial_id"] == record.get("trial_id")
                and trial["target_uid"] == record["uid"],
                "Observed fault is not bound to the active trial.",
            )
            trial["status"] = "observed"
            trial["observed_at"] = observed_at
            trial["phases"]["detect"] = observed_at
            trial["phases"]["collect"] = observed_at
            trial["observation_sha256"] = record["observation_sha256"]
            if trial["scenario_kind"] == "recovery":
                trial["load"].setdefault("observations", []).append(
                    {
                        "phase": "fault-observation",
                        **self.offered_rate_snapshot(trial["target_uid"], trial["load"]["rate"]),
                    }
                )
            private_write(self.trial_path, json.dumps(trial, indent=2) + "\n")
        self.receipt("observe", observation)

    def renew_readers(self):
        for name in ("k3s", "prometheus"):
            require(
                self.service(name)["State"]["Running"],
                "Read-only renewal needs the owned source services already running.",
            )
        for name in self.marker["deployments"]:
            self.deployment(name)
        for kind in ("reader", "monitoring"):
            self.credential(kind)
        return {
            "identities": ["reader", "monitoring"],
            "services_restarted": False,
            "writer_authority_changed": False,
        }

    def renew(self, kind):
        self.volume_guard()
        self.dc(
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "240",
            "db",
            timeout=300,
        )
        self.start_k3s()
        if kind == "writer":
            self.dc(
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                "120",
                "api",
                "executor",
                timeout=180,
            )
        for name in self.marker["deployments"]:
            self.deployment(name)
        if kind == "writer":
            self.no_execution()
            self.maintenance("enter_recovery")
        self.credential(kind)
        if kind in {"reader", "writer"}:
            services = APP_SERVICES if kind == "reader" else ("executor",)
            self.dc("restart", *services)
        else:
            self.dc(*namespace_recreate_arguments())
        self.receipt(
            "renew",
            {
                "identity": kind,
                "expiry": json.loads((self.directory / kind / "expiry.json").read_text()),
                "writer": "renewal never enables dispatch",
            },
        )

    def cleanup(self, confirm):
        require(
            confirm == self.marker["owner"],
            "Supply the exact owner UUID to authorize deletion of owned lab data.",
        )
        inventory = self.ownership()
        require(
            not any(item["State"]["Running"] for item in inventory["container"]),
            "Run stop successfully before cleanup.",
        )
        removed = {}
        for kind in ("container", "network", "volume"):
            ids = [item["Id"] if kind != "volume" else item["Name"] for item in inventory[kind]]
            self.ownership()
            for identifier in ids:
                self.command(["docker", kind, "rm", identifier])
            removed[kind] = ids
        self.receipt(
            "cleanup",
            {
                "removed": removed,
                "generated_private_files": "retained locally; remove this exact directory separately if no longer needed",
            },
        )


def main():
    if os.environ.get("CLOUDOPS_RELEASE_BUNDLE"):
        raise SystemExit(
            "The retained owned lab builds its own images; unset CLOUDOPS_RELEASE_BUNDLE."
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(OWNED_PROJECTS), required=True)
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in (
        "preflight",
        "start",
        "resume",
        "status",
        "verify",
        "denials",
        "observe",
        "stop",
        "recreate",
        "replace-history",
        "renew-readers",
    ):
        sub.add_parser(name)
    containment = sub.add_parser("containment")
    containment.add_argument("--read-only", action="store_true")
    for name in ("collect", "stored"):
        product = sub.add_parser(name)
        product.add_argument("--password-stdin", action="store_true", required=True)
    load = sub.add_parser("load")
    load.add_argument("--target", choices=["checkout", "history", "both"], default="checkout")
    load.add_argument("--seconds", type=int, default=90)
    load.add_argument("--rate", type=int, default=5)
    load.add_argument(
        "--recovery",
        action="store_true",
        help="Start the owned finite recovery load bound to a ready E10 trial.",
    )
    load.add_argument(
        "--detach",
        action="store_true",
        help="Keep the bounded recovery load running while the product action is exercised.",
    )
    load_stop = sub.add_parser("load-stop")
    load_stop.add_argument(
        "--interrupt",
        action="store_true",
        help="Stop the exact active E10 recovery load before its fixed duration ends.",
    )
    fault = sub.add_parser("fault")
    fault.add_argument(
        "scenario",
        choices=[
            "crashloop",
            "oom",
            "traffic",
            "pending",
            "imagepull",
            "init-retry",
            *RECOVERY_FAULTS.values(),
        ],
    )
    reset = sub.add_parser("reset")
    reset.add_argument(
        "--interrupted",
        action="store_true",
        help="Record that this reset resumed an interrupted trial state.",
    )
    trial = sub.add_parser("trial")
    trial_commands = trial.add_subparsers(dest="trial_operation", required=True)
    begin = trial_commands.add_parser("begin")
    begin.add_argument("scenario_id")
    begin.add_argument("--qualification-receipt", type=Path)
    post_check = trial_commands.add_parser("post-check")
    post_check.add_argument("--execution-receipt", type=Path, required=True)
    post_check.add_argument("--verification-receipt", type=Path, required=True)
    renew = sub.add_parser("renew")
    renew.add_argument("identity", choices=["reader", "writer", "monitoring"])
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--confirm-owner", required=True)
    sub.add_parser(
        "recovery-config", help="Write the trusted E07 policy/profiles for the recovery lab."
    )
    qualify = sub.add_parser(
        "qualify", help="Calibrate one recovery scenario's exact post-action state."
    )
    qualify.add_argument("scenario_id", choices=sorted(RECOVERY_FAULTS))
    qualify.add_argument("--rate", type=int, required=True)
    qualify.add_argument("--seconds", type=int, default=240)
    writer = sub.add_parser("writer", help="Open or re-quarantine the isolated writer.")
    writer.add_argument("writer_operation", choices=["enable", "quarantine"])
    args = parser.parse_args()
    require(
        args.project == OWNED_PROJECT,
        "Select this owned project with CLOUDOPS_LAB_PROJECT as well as --project.",
    )
    require(
        1 <= getattr(args, "seconds", 1) <= 21600 and 1 <= getattr(args, "rate", 1) <= 30,
        "Load bounds exceeded.",
    )
    lab = Lab(args.directory)
    if args.operation == "preflight":
        print(json.dumps(lab.preflight(), indent=2))
        return
    lab.ownership(allow_new=args.operation == "start")
    if args.operation == "start" and lab.marker is None:
        lab.initialize(lab.preflight())
    require(lab.marker is not None, "Initialize through start first.")
    require(
        not lab.directory.stat().st_mode & 0o077,
        "Generated directory must remain private.",
    )
    with (lab.directory / "operation.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lab.ownership()
        if args.operation == "status":
            inventory = lab.resources()
            print(
                json.dumps(
                    {
                        "owner": lab.marker["owner"],
                        "deployments": lab.marker["deployments"],
                        "containers": [
                            {
                                "id": item["Id"],
                                "service": item["Config"]["Labels"].get(
                                    "com.docker.compose.service"
                                ),
                                "running": item["State"]["Running"],
                            }
                            for item in inventory["container"]
                        ],
                    },
                    indent=2,
                )
            )
        elif args.operation == "load":
            lab.load(
                args.target,
                args.seconds,
                args.rate,
                recovery=args.recovery,
                detach=args.detach,
            )
        elif args.operation == "load-stop":
            lab.finish_recovery_load(interrupt=args.interrupt)
        elif args.operation == "fault":
            lab.fault(args.scenario)
        elif args.operation == "reset":
            lab.reset(interrupted=args.interrupted)
        elif args.operation == "trial":
            if args.trial_operation == "begin":
                lab.begin_trial(args.scenario_id, args.qualification_receipt)
            elif args.trial_operation == "post-check":
                lab.recovery_post_check(args.execution_receipt, args.verification_receipt)
        elif args.operation == "renew":
            lab.renew(args.identity)
        elif args.operation == "renew-readers":
            lab.receipt("renew-readers", lab.renew_readers())
        elif args.operation == "recovery-config":
            lab.recovery_config()
        elif args.operation == "qualify":
            lab.qualify(args.scenario_id, args.rate, args.seconds)
        elif args.operation == "writer":
            lab.writer(args.writer_operation)
        elif args.operation == "cleanup":
            lab.cleanup(args.confirm_owner)
        elif args.operation == "containment":
            lab.receipt("containment", lab.containment(read_only=args.read_only))
        elif args.operation in {"collect", "stored"}:
            password = sys.stdin.readline().rstrip("\r\n")
            require(
                12 <= len(password) <= 256,
                "Provide the existing Operator password on stdin.",
            )
            lab.stored(password, create=args.operation == "collect")
        elif args.operation == "replace-history":
            lab.replace_history()
        else:
            getattr(lab, args.operation)()


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            f"Lab operation stopped: {error if isinstance(error, RuntimeError) else type(error).__name__}. No additional authority granted.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
