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

from local_lab import OWNED_PROJECT, ROOT, private_write

DIRECTORY = ROOT / "tmp/local-testing-lab"
OWNER_LABEL = "io.cloudops.lab.owner"
GIB = 1024**3
SAMPLE_IMAGE = (
    "cloudops-local-testing-lab-sample:"
    + hashlib.sha256(
        b"".join(
            (ROOT / file).read_bytes()
            for file in (
                "config/lab/Dockerfile.sample",
                "config/lab/sample_app.py",
                "config/lab/load.py",
            )
        )
    ).hexdigest()[:16]
)
SAMPLE_CTR_IMAGE = "docker.io/library/" + SAMPLE_IMAGE
APP_SERVICES = ("api", "worker", "observer", "executor")
FILES = (
    "compose.yaml",
    "compose.https.yaml",
    "compose.integration.yaml",
    "compose.alertmanager.yaml",
    "compose.lab.yaml",
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
    require(parent is not None, "K3s PID has no exact container cgroup ancestor; stress blocked.")
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
            "intake-api",
            "intake-sender",
            "prometheus",
            "monitoring",
            "receipts",
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
            not folder.stat().st_mode & 0o077, "Docker configuration directory must be private."
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
        from cloudops.features.workloads.scope_config import LiveScope, cluster_fingerprint

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
            client_config = {"server": "https://k3s:6443", "ca_data": ca, "token": token}
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

    def credentials_valid(self):
        for kind in ("reader", "writer", "monitoring"):
            expiry = json.loads((self.directory / kind / "expiry.json").read_text())["expires_at"]
            require(
                datetime.fromisoformat(expiry.replace("Z", "+00:00")) > datetime.now(timezone.utc),
                f"{kind} credential expired; renew explicitly.",
            )
        for kind in ("reader", "writer"):
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
        from cloudops.features.workloads.scope_config import LiveScope, cluster_fingerprint

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
                "node-ip-restart", {"container_id": node["Id"], "change": change, "retries": 1}
            )

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
        self.dc("up", "-d", "--no-deps", "--wait", "--wait-timeout", "240", "db", timeout=300)
        self.start_k3s()
        self.volume_guard(remember=True)
        archive = self.directory / "sample.tar"
        try:
            with archive.open("wb") as output:
                self.command(["docker", "save", SAMPLE_IMAGE], stdout=output)
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
            image = json.loads(self.command(["docker", "image", "inspect", container["Image"]]))[0]
            values[service] = {
                "image_id": container["Image"],
                "repo_digests": image.get("RepoDigests", []),
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

    def containment(self):
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
                        and quantity(limits.get("memory", str(4 * GIB))) <= 160 * 1024**2,
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
                        limit != "max" and int(limit) <= 160 * 1024**2,
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

    def verify(self):
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
        require(
            targets and all(target["health"] == "up" for target in targets),
            "A discovery/kubelet/metrics target is missing or unhealthy; inspect private Prometheus target diagnostics.",
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
                    if row["metric"].get("deployment_uid") in self.marker["deployments"].values()
                ),
                "Metric has no finite observation; load both workloads and warm the window.",
            )
            queries[metric] = [{"labels": row["metric"], "value": row["value"]} for row in rows]
        self.receipt(
            "verify",
            {
                "network_attachment": binding,
                "targets": [
                    {"job": target["labels"]["job"], "health": target["health"]}
                    for target in targets
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
            },
        )

    def load(self, target, seconds, rate):
        proof = self.containment()
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

    def fault(self, scenario):
        require(
            not (self.directory / "fault.json").exists(),
            "Reset the recorded fault before another injection.",
        )
        self.no_execution()
        proof = self.containment()
        before = self.deployment("checkout")
        fields = owned_fields(before)
        wanted = copy.deepcopy(fields)
        if scenario in {"crashloop", "oom", "traffic"}:
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
        private_write(self.directory / "fault.json", json.dumps(record))
        self.patch(before, field_changes(before, wanted))
        record["state"] = "injected"
        private_write(self.directory / "fault.json", json.dumps(record))
        self.receipt(
            "fault",
            {
                "target_uid": record["uid"],
                "containment": proof,
                "observation": "Run observe to require actual runtime evidence; injection is not a pass.",
            },
        )

    def reset(self):
        self.no_execution()
        path = self.directory / "fault.json"
        require(path.is_file(), "No recorded fault; refusing an unspecified reset.")
        record = json.loads(path.read_text())
        live = self.deployment("checkout")
        current = owned_fields(live)
        require(
            current in (record["expected"], record["before"]),
            "Owned fields changed after injection; manual inspection required.",
        )
        require(
            live["metadata"]["uid"] == record["uid"],
            "Fault target was replaced; reset refused.",
        )
        if current != record["before"]:
            self.patch(live, field_changes(live, record["before"]))
        self.kube(
            "rollout",
            "status",
            "deployment/checkout",
            "-n",
            "demo",
            "--timeout=180s",
            timeout=200,
        )
        private_write(
            self.directory / "receipts" / f"{time.time_ns()}-reset-private.json",
            json.dumps(record),
        )
        require(
            owned_fields(self.deployment("checkout")) == record["before"],
            "Reset fields changed before completion; keep the fault record.",
        )
        path.unlink()
        self.receipt(
            "reset",
            {
                "target_uid": record["uid"],
                "restored_owned_fields": True,
                "history": "preserved",
                "writer": "still quarantined",
            },
        )

    def observe(self):
        record = json.loads((self.directory / "fault.json").read_text())
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
                    if record["scenario"] == "oom" and terminated == "OOMKilled":
                        return pod
                    if (
                        record["scenario"] in {"crashloop", "init-retry"}
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
            if record["scenario"] == "traffic":
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
        if record["scenario"] in {"oom", "crashloop", "init-retry"}:
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
        self.receipt(
            "observe",
            {
                "target_uid": record["uid"],
                "status": pod["status"],
                "events": [
                    {"reason": event.get("reason"), "uid": event["metadata"]["uid"]}
                    for event in events
                ],
                "logs": logs,
                "raw_logs_timestamped_nonempty": all(log["timestamped_nonempty"] for log in logs)
                if logs
                else None,
                "remaining": "Compare stored collector sources with these exact Pod/container/window/options; no completeness waiver.",
            },
        )

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=[OWNED_PROJECT], required=True)
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in (
        "preflight",
        "start",
        "resume",
        "status",
        "verify",
        "containment",
        "denials",
        "observe",
        "reset",
        "stop",
        "recreate",
        "replace-history",
    ):
        sub.add_parser(name)
    for name in ("collect", "stored"):
        product = sub.add_parser(name)
        product.add_argument("--password-stdin", action="store_true", required=True)
    load = sub.add_parser("load")
    load.add_argument("--target", choices=["checkout", "history", "both"], default="checkout")
    load.add_argument("--seconds", type=int, default=90)
    load.add_argument("--rate", type=int, default=5)
    fault = sub.add_parser("fault")
    fault.add_argument(
        "scenario",
        choices=["crashloop", "oom", "traffic", "pending", "imagepull", "init-retry"],
    )
    renew = sub.add_parser("renew")
    renew.add_argument("identity", choices=["reader", "writer", "monitoring"])
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--confirm-owner", required=True)
    args = parser.parse_args()
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
            lab.load(args.target, args.seconds, args.rate)
        elif args.operation == "fault":
            lab.fault(args.scenario)
        elif args.operation == "renew":
            lab.renew(args.identity)
        elif args.operation == "cleanup":
            lab.cleanup(args.confirm_owner)
        elif args.operation == "containment":
            lab.receipt("containment", lab.containment())
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
