"""Prepare/clean fixed local K3s observation fixtures without changing writer authority."""

import argparse
import copy
import hashlib
import json
import os
import subprocess
import asyncio
import time
from datetime import datetime, timezone
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


def verify():
    """Fault only the labeled fixture; never alter checkout or the writer allowlist."""
    if urlsplit(os.environ.get("DATABASE_URL", "")).path != "/cloudops27_lab":
        raise RuntimeError("Use the separate cloudops27_lab PostgreSQL database.")
    from sqlalchemy import select
    from cloudops.features.workloads.models import Workload
    from cloudops.features.workloads.observation_service import detail, observe_direct
    from cloudops.shared.database import sessions

    os.environ["KUBE_READ_CONFIG"] = str(FOLDER / "client.json")
    expected = json.loads((FOLDER / "original.json").read_text())
    assert original() == expected
    with sessions()() as db:
        identity = db.scalar(
            select(Workload.id).where(Workload.name == "scope27-ready", Workload.active)
        )
    if identity is None:
        raise RuntimeError("Prepare the isolated scope before verification.")

    def observe():
        for _ in range(4):
            result = asyncio.run(observe_direct(identity))
            if result.outcome != "busy":
                return result
            time.sleep(1)
        raise RuntimeError("Observer busy; stop scheduled polling before this bounded probe.")

    def pod():
        values = json.loads(
            kubectl("get", "pods", "-n", "demo", "-l", "app=scope27-ready", "-o", "json")
        )["items"]
        owned = [
            p
            for p in values
            if any(
                o.get("kind") == "ReplicaSet" and o.get("controller")
                for o in p["metadata"].get("ownerReferences", [])
            )
            and not p["metadata"].get("deletionTimestamp")
        ]
        return owned[0] if len(owned) == 1 else None

    def wait_for(predicate):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            value = pod()
            if (
                value
                and predicate(value)
                and any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in value.get("status", {}).get("conditions", [])
                )
            ):
                kubectl(
                    "rollout", "status", "deployment/scope27-ready", "-n", "demo", "--timeout=20s"
                )
                return value
            time.sleep(1)
        raise RuntimeError("The labeled fixture did not recover within the probe deadline.")

    before = observe()
    assert before.outcome == "complete" and before.observation.health == "healthy"
    value = pod()
    initial_uid = value["metadata"]["uid"]
    initial_count = value["status"]["containerStatuses"][0]["restartCount"]
    # A Job supplies a real non-ReplicaSet owner while matching the observed RS labels.
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "scope27-unrelated",
            "namespace": "demo",
            "labels": {"cloudops-probe": "observation27"},
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": value["metadata"]["labels"]},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "wait",
                            "image": value["spec"]["containers"][0]["image"],
                            "command": ["python", "-c", "import time; time.sleep(600)"],
                        }
                    ],
                },
            },
        },
    }
    kubectl("apply", "-f", "-", body=json.dumps(job))
    kubectl(
        "wait",
        "--for=condition=Ready",
        "pod",
        "-n",
        "demo",
        "-l",
        "job-name=scope27-unrelated",
        "--timeout=60s",
    )
    unrelated = json.loads(
        kubectl("get", "pods", "-n", "demo", "-l", "job-name=scope27-unrelated", "-o", "json")
    )["items"][0]
    ownership = observe()
    assert ownership.observation.health == "healthy"
    assert unrelated["metadata"]["uid"] not in {p.uid for p in ownership.observation.pods}
    print("Matching-label Job Pod excluded by actual owner UID.", flush=True)
    try:
        kubectl(
            "exec",
            value["metadata"]["name"],
            "-n",
            "demo",
            "--",
            "python",
            "-c",
            "import os; os.kill(1, 9)",
        )
    except subprocess.CalledProcessError as error:
        if error.returncode != 137:
            raise
    restarted = wait_for(
        lambda p: p["metadata"]["uid"] == initial_uid
        and p.get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", -1)
        > initial_count
    )
    after = observe()
    assert after.outcome == "complete" and after.observation.health == "healthy"
    with sessions()() as db:
        comparison = detail(db, identity).comparison
    assert comparison.restart_delta is not None and comparison.restart_delta >= 1
    print("Same Pod/container restart measured without inventing a cause.", flush=True)
    kubectl("delete", "pod", restarted["metadata"]["name"], "-n", "demo", "--wait=false")
    replacement = wait_for(lambda p: p["metadata"]["uid"] != initial_uid)
    replaced = observe()
    assert replaced.outcome == "complete"
    with sessions()() as db:
        changed = detail(db, identity).comparison
    assert changed.state == "coverage_changed" and changed.restart_delta is None
    print("Replacement Pod changes coverage and retains a null restart delta.", flush=True)
    os.environ["KUBE_READ_CONFIG"] = str(FOLDER / "denied.json")
    try:
        denied = observe()
        assert denied.outcome == "failed" and denied.reason == "permission_denied"
        with sessions()() as db:
            failed = detail(db, identity)
        assert failed.summary.effective_health == "unknown" and failed.historical is not None
    finally:
        os.environ["KUBE_READ_CONFIG"] = str(FOLDER / "client.json")
    restored = observe()
    assert restored.outcome == "complete" and restored.observation.health == "healthy"
    assert original() == expected
    receipt = {
        "runtime_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "environment": "Real scoped local K3s over verified TLS and PostgreSQL; host producer",
        "workload_id": str(identity),
        "before_observation_id": str(before.observation.observation_id),
        "restart_observation_id": str(after.observation.observation_id),
        "restart_delta": comparison.restart_delta,
        "initial_pod_uid": initial_uid,
        "replacement_pod_uid": replacement["metadata"]["uid"],
        "replacement_comparison": changed.model_dump(mode="json"),
        "read_denial": denied.reason,
        "historical_after_denial": str(failed.historical.observation_id),
        "recovery_observation_id": str(restored.observation.observation_id),
        "matching_label_job_excluded": True,
        "protected_checkout_uid_spec_writer_policy_unchanged": True,
    }
    (FOLDER / "live-results.json").write_text(json.dumps(receipt, indent=2))
    print("Live workload checks passed; sanitized receipt saved under tmp/observation27.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "verify", "cleanup"])
    parser.add_argument("--scope-size", type=int, choices=range(3, 101), default=100)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.scope_size)
    elif args.command == "verify":
        verify()
    else:
        cleanup()
