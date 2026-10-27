"""Prepare/clean fixed local K3s observation fixtures without changing writer authority."""

import argparse
import copy
import hashlib
import json
import os
import subprocess
from urllib.parse import urlsplit

from local_lab import COMPOSE, ROOT

FOLDER = ROOT / "tmp/observation27"
LABEL = "cloudops-probe=observation27"


def kubectl(*args, body=None):
    return subprocess.run(
        [*COMPOSE, "exec", "-T", "k3s", "/bin/kubectl", *args],
        input=body,
        text=True,
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def original():
    deployment = json.loads(kubectl("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    return {
        "uid": deployment["metadata"]["uid"],
        "spec_digest": digest(deployment["spec"]),
        "writer_digest": hashlib.sha256(
            (ROOT / "tmp/e01-integration/writer/workloads.json").read_bytes()
        ).hexdigest(),
    }


def prepare(size):
    if urlsplit(os.environ.get("DATABASE_URL", "")).path != "/cloudops27_lab":
        raise RuntimeError("Use the separate cloudops27_lab PostgreSQL database.")
    from alembic import command
    from alembic.config import Config

    from cloudops.features.workloads.service import sync_config
    from cloudops.shared.config import settings
    from cloudops.shared.database import sessions

    FOLDER.mkdir(parents=True, exist_ok=True)
    initial = original()
    previous = FOLDER / "original.json"
    if previous.exists() and json.loads(previous.read_text()) != initial:
        raise RuntimeError("Protected checkout/writer state differs from the prior probe receipt.")
    previous.write_text(json.dumps(initial, indent=2))
    deployment = json.loads(kubectl("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    values = []
    for index in range(size):
        name = (
            "scope27-ready"
            if index == 0
            else "scope27-unready"
            if index == 1
            else f"scope27-zero-{index:03}"
        )
        template = copy.deepcopy(deployment["spec"]["template"])
        template["metadata"] = {"labels": {"app": name, "cloudops-probe": "observation27"}}
        template["spec"]["containers"][0]["name"] = "app"
        if index == 1:
            template["spec"]["containers"][0]["readinessProbe"]["httpGet"]["port"] = 1
        values.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": "demo",
                    "labels": {"cloudops-probe": "observation27"},
                },
                "spec": {
                    "replicas": 1 if index < 2 else 0,
                    "selector": {"matchLabels": {"app": name}},
                    "template": template,
                },
            }
        )
    kubectl(
        "apply", "-f", "-", body=json.dumps({"apiVersion": "v1", "kind": "List", "items": values})
    )
    kubectl("rollout", "status", "deployment/scope27-ready", "-n", "demo", "--timeout=90s")
    actual = json.loads(kubectl("get", "deployments", "-n", "demo", "-l", LABEL, "-o", "json"))[
        "items"
    ]
    if len(actual) != size or any(not d["metadata"]["name"].startswith("scope27-") for d in actual):
        raise RuntimeError("Unexpected fixture inventory; inspect before proceeding.")
    scope = [
        {
            "cluster": "local-k3s",
            "namespace": "demo",
            "name": d["metadata"]["name"],
            "uid": d["metadata"]["uid"],
            "source": "live_kubernetes",
        }
        for d in actual
    ]
    policy = FOLDER / "workloads.json"
    policy.write_text(json.dumps(scope, indent=2))
    reader = json.loads(
        (ROOT / "tmp/e01-integration/reader/client.json").read_text(encoding="utf-8")
    )
    reader["server"] = "https://127.0.0.1:6445"
    reader["token"] = kubectl("create", "token", "cloudops-reader", "-n", "demo", "--duration=8h")
    (FOLDER / "client.json").write_text(json.dumps(reader))
    denied = {
        **reader,
        "token": kubectl("create", "token", "default", "-n", "demo", "--duration=1h"),
    }
    (FOLDER / "denied.json").write_text(json.dumps(denied))
    os.environ["WORKLOADS_FILE"] = str(policy)
    settings.cache_clear()
    command.upgrade(Config(str(ROOT / "services/backend/alembic.ini")), "head")
    with sessions()() as db:
        sync_config(db)
    assert original() == initial
    print(
        json.dumps(
            {
                "prepared": size,
                "mix": {"ready": 1, "unready": 1, "scaled_to_zero": size - 2},
                "protected_checkout_unchanged": True,
            }
        )
    )


def cleanup():
    expected = json.loads((FOLDER / "original.json").read_text())
    if original() != expected:
        raise RuntimeError("Protected checkout/writer state changed; inspect before cleanup.")
    for resource in ("deployments", "jobs"):
        values = json.loads(kubectl("get", resource, "-n", "demo", "-l", LABEL, "-o", "json"))[
            "items"
        ]
        for value in values:
            name = value["metadata"]["name"]
            if not name.startswith("scope27-"):
                raise RuntimeError("Refusing to delete an unexpected fixture name.")
            kubectl("delete", resource, name, "-n", "demo", "--wait=false")
    assert original() == expected
    print("Only labeled scope27 fixtures removed; protected checkout and writer policy preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "cleanup"])
    parser.add_argument("--scope-size", type=int, choices=range(3, 101), default=100)
    args = parser.parse_args()
    prepare(args.scope_size) if args.command == "prepare" else cleanup()
