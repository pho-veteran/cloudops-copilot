"""Shared mechanics for the fixed E02 local probes; no production runtime imports it."""

import argparse
from datetime import datetime, timezone
from getpass import getpass
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
import time

import httpx

spec = importlib.util.spec_from_file_location(
    "e02_local_lab", Path(__file__).with_name("probe-e02-integration.py")
)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def arguments(description, filename):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--ca", type=Path, default=lab.folder / "root.crt")
    parser.add_argument("--operator", default="e02-operator")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--output", type=Path, default=lab.folder / filename)
    result = parser.parse_args()
    lab.require(__debug__, "Run probes without -O/PYTHONOPTIMIZE.")
    lab.require(
        bool(os.environ.get("POSTGRES_PASSWORD")), "Set the disposable lab POSTGRES_PASSWORD."
    )
    return result


def password(args):
    return (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass("E02 Operator password: ")
    )


def client(args):
    return httpx.Client(
        base_url=lab.origin,
        verify=ssl.create_default_context(cafile=str(args.ca)),
        trust_env=False,
        timeout=25,
    )


def login(connection, args, secret):
    response = connection.post(
        "/api/login",
        json={"username": args.operator, "password": secret},
        headers={"Origin": lab.origin},
    )
    lab.require(response.status_code == 200, "Operator sign-in failed.")
    return {"Origin": lab.origin, "X-CSRF-Token": response.json()["csrf_token"]}


def wait_ready(connection):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            if connection.get("/api/health/ready").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("The local API did not become ready.")


def metadata(probe, *extra):
    expected = json.loads((lab.folder / "protected.json").read_text())
    lab.require(lab.protected() == expected, "Protected original checkout/writer policy changed.")
    return lab.receipt_metadata(Path(__file__), probe, *extra)


def save(report, output):
    lab.require(
        lab.protected() == json.loads((lab.folder / "protected.json").read_text()),
        "Protected original checkout/writer policy changed during the probe.",
    )
    lab.require(
        lab.git_bytes("rev-parse", "HEAD").decode().strip() == report["runtime_commit"],
        "Candidate commit changed during verification.",
    )
    lab.require(
        not lab.git_bytes("status", "--porcelain=v1", "--untracked-files=all").strip(),
        "Checkout changed during verification.",
    )
    report["original_checkout_uid_spec_writer_policy_unchanged"] = True
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "receipt": str(output),
                "runtime_commit": report["runtime_commit"],
                "result": "passed",
                "browser_gate": "open",
            }
        )
    )


def owned_pod(name):
    deployment = lab.read_fixture("Deployment", name)
    lab.require(deployment is not None, "Prepared fixture disappeared.")
    values = json.loads(lab.kube("get", "pods", "-n", "demo", "-l", "app=" + name, "-o", "json"))[
        "items"
    ]
    matched = []
    for pod in values:
        if pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") in {
            "Succeeded",
            "Failed",
        }:
            continue
        owners = [
            o
            for o in pod["metadata"].get("ownerReferences", [])
            if o.get("controller") and o.get("kind") == "ReplicaSet"
        ]
        if len(owners) != 1:
            continue
        owner = owners[0]
        raw = lab.kube(
            "get", "replicaset", owner["name"], "-n", "demo", "-o", "json", "--ignore-not-found"
        )
        if not raw:
            continue
        rs = json.loads(raw)
        parents = [
            o
            for o in rs["metadata"].get("ownerReferences", [])
            if o.get("controller")
            and o.get("kind") == "Deployment"
            and o.get("uid") == deployment["metadata"]["uid"]
        ]
        if rs["metadata"]["uid"] == owner["uid"] and len(parents) == 1:
            matched.append(pod)
    return matched[0] if len(matched) == 1 else None


def delete_owned_pod(name, pod):
    current = owned_pod(name)
    lab.require(
        current is not None and current["metadata"]["uid"] == pod["metadata"]["uid"],
        "Owned Pod identity changed before deletion.",
    )
    lab.kube(
        "delete",
        "--raw",
        "/api/v1/namespaces/demo/pods/" + current["metadata"]["name"],
        "-f",
        "-",
        body={
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {key: current["metadata"][key] for key in ("uid", "resourceVersion")},
        },
    )
