"""Exercise E02 against fixed disposable local fixtures; never mutate demo/checkout.

Use the documented lab setup and temporary CA lifecycle. Database records and all
named Docker volumes are retained. This is an integration probe, not a release SLO.
"""

import argparse
import base64
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import httpx
from local_lab import ROOT as root, COMPOSE
from cloudops.features.workloads.scope_config import cluster_fingerprint

folder = root / "tmp/e02-baselines"
origin = "https://localhost:8443"
compose = [*COMPOSE, "-f", str(folder / "compose.json")]


def call(*args, input=None):
    return command(list(args), input)


def passwords_for(args):
    return {
        role: sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass(f"E02 {role} password: ")
        for role in ("operator", "viewer")
    }


def command(args, body=None, check=True):
    return subprocess.run(
        args,
        input=body,
        stdin=subprocess.DEVNULL if body is None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        env=os.environ,
        cwd=root,
    ).stdout.strip()


def kube(*args, body=None):
    return command(
        ["docker", "exec", "-i", "cloudops-e01-https-k3s-1", "/bin/kubectl", *args],
        json.dumps(body) if body else None,
    )


def app(code):
    return command([*compose, "exec", "-T", "api", "python", "-c", code])


def sync():
    return command(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "cloudops.entrypoints.cli",
            "sync-workloads",
        ]
    )


def get(name):
    return json.loads(kube("get", "deployment", name, "-n", "demo", "-o", "json"))


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def protected():
    checkout = get("checkout")
    return {
        "uid": checkout["metadata"]["uid"],
        "spec_sha256": hash_value(checkout["spec"]),
        "writer_sha256": hashlib.sha256(
            (root / "tmp/e01-integration/writer/workloads.json").read_bytes()
        ).hexdigest(),
    }


def domain():
    return json.loads(
        app(
            "import json; from sqlalchemy import select; from cloudops.shared.database import sessions; from cloudops.features.recovery.models import ActionPlan,Execution,PlanReview; from cloudops.shared.jobs import Job; from cloudops.features.incidents.models import Incident;\nwith sessions()() as db: print(json.dumps({m.__tablename__:sorted([repr(dict(r)) for r in db.execute(select(m.__table__)).mappings()]) for m in [ActionPlan,Execution,PlanReview,Job,Incident]}))"
        )
    )


def live_read(wid):
    for _ in range(8):
        result = json.loads(
            app(
                f"import asyncio; from uuid import UUID; from cloudops.features.workloads.observation_service import observe_direct; r=asyncio.run(observe_direct(UUID('{wid}'),'recovery_context')); print(r.model_dump_json())"
            )
        )
        if result["outcome"] != "busy":
            return result
        time.sleep(0.5)
    raise RuntimeError("Direct reader remained busy")


def stable(value):
    return {k: v for k, v in value.items() if k != "age_seconds"}


def prepare(args):
    folder.mkdir(parents=True, exist_ok=True)
    checkout = json.loads(kube("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    protected = {
        "uid": checkout["metadata"]["uid"],
        "spec_sha256": hashlib.sha256(
            json.dumps(checkout["spec"], sort_keys=True).encode()
        ).hexdigest(),
        "writer_sha256": hashlib.sha256(
            (root / "tmp/e01-integration/writer/workloads.json").read_bytes()
        ).hexdigest(),
    }
    prior = folder / "protected.json"
    if prior.exists():
        assert json.loads(prior.read_text()) == protected
    else:
        prior.write_text(json.dumps(protected, indent=2))
    for name in ["e02-ready", "e02-recreate", "e02-config", "e02-unlisted"]:
        existing = subprocess.run(
            [
                "docker",
                "exec",
                "cloudops-e01-https-k3s-1",
                "/bin/kubectl",
                "get",
                "deployment",
                name,
                "-n",
                "demo",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        if existing.returncode == 0:
            assert (
                json.loads(existing.stdout)["metadata"]["labels"].get("cloudops-probe")
                == "e02-baselines"
            )
            continue
        container = {
            "name": "app",
            "image": checkout["spec"]["template"]["spec"]["containers"][0]["image"],
            "command": ["python", "-m", "http.server", "8080"],
            "resources": {
                "requests": {"cpu": "10m", "memory": "32Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            },
            "readinessProbe": {
                "tcpSocket": {"port": 8080},
                "periodSeconds": 1,
                "timeoutSeconds": 1,
            },
            "env": [{"name": "LITERAL_SENTINEL", "value": "private-literal-e02"}],
        }
        if name == "e02-config":
            kube(
                "apply",
                "-f",
                "-",
                body={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "namespace": "demo",
                        "name": "e02-settings",
                        "labels": {"cloudops-probe": "e02-baselines"},
                    },
                    "data": {"MESSAGE": "private-config-e02"},
                },
            )
            container["envFrom"] = [{"configMapRef": {"name": "e02-settings"}}]
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "namespace": "demo",
                "name": name,
                "labels": {"cloudops-probe": "e02-baselines"},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [container]},
                },
            },
        }
        (folder / f"{name}.json").write_text(json.dumps(manifest, indent=2))
        kube("apply", "-f", "-", body=manifest)
        kube("rollout", "status", f"deployment/{name}", "-n", "demo", "--timeout=90s")
    ca = call(
        "docker",
        "exec",
        "cloudops-e01-https-k3s-1",
        "cat",
        "/var/lib/rancher/k3s/server/tls/server-ca.crt",
    )
    reader = {
        "server": "https://k3s:6443",
        "token": kube("create", "token", "cloudops-reader", "-n", "demo", "--duration=8h"),
        "ca_data": base64.b64encode((ca + "\n").encode()).decode(),
    }
    (folder / "client.json").write_text(json.dumps(reader))
    denied = {**reader, "token": kube("create", "token", "default", "-n", "demo", "--duration=8h")}
    (folder / "denied.json").write_text(json.dumps(denied))
    scope = {
        "schema_version": 2,
        "cluster": {
            "key": "e02-local-k3s",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": cluster_fingerprint(reader),
        },
        "deployments": [
            {"namespace": "demo", "name": n}
            for n in ["checkout", "e02-ready", "e02-recreate", "e02-config"]
        ],
    }
    (folder / "workloads.json").write_text(json.dumps(scope, indent=2))
    dbs = call(
        "docker",
        "exec",
        "cloudops-e01-https-db-1",
        "psql",
        "-U",
        "cloudops",
        "-d",
        "postgres",
        "-tAc",
        "SELECT datname FROM pg_database WHERE datname='cloudops28_lab'",
    )
    if not dbs:
        call(
            "docker",
            "exec",
            "cloudops-e01-https-db-1",
            "psql",
            "-U",
            "cloudops",
            "-d",
            "postgres",
            "-c",
            "CREATE DATABASE cloudops28_lab",
        )
    backend = {
        "image": args.backend_image,
        "environment": {
            "DATABASE_URL": f"postgresql+psycopg://cloudops:{quote(os.environ['POSTGRES_PASSWORD'], safe='')}@db:5432/cloudops28_lab"
        },
        "volumes": [f"{folder.as_posix()}:/run/cloudops-reader:ro"],
    }
    override = {"services": {name: backend for name in ["api", "migrate", "worker", "observer"]}}
    override["services"]["https"] = {"image": args.https_image}
    (folder / "compose.json").write_text(json.dumps(override, indent=2))
    print(
        json.dumps(
            {
                "prepared": 4,
                "scoped_disposable": 3,
                "unlisted": 1,
                "protected_checkout": protected["uid"],
                "writer_policy_unchanged": True,
            }
        )
    )


def verify(args):
    passwords = passwords_for(args)
    actual = json.loads(
        app(
            "import hashlib,json; from pathlib import Path; p=Path('/workspace/services/backend/src/cloudops'); print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*.py')}))"
        )
    )
    package = root / "services/backend/src/cloudops"
    expected_sources = {
        str(path.relative_to(package)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in package.rglob("*.py")
    }
    assert actual == expected_sources, (
        "Running backend source differs from this checkout; rebuild before collecting evidence."
    )
    report = {
        "runtime_commit": command(["git", "rev-parse", "HEAD"]),
        "environment": "real K3s + PostgreSQL + HTTPS; disposable e02-* Deployments",
        "checks": {},
    }
    report["source_sha256"] = expected_sources
    report["probe_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    before_protected = protected()
    assert before_protected == json.loads((folder / "protected.json").read_text())
    reader = (folder / "client.json").read_bytes()
    policy = (folder / "workloads.json").read_bytes()
    tls = ssl.create_default_context(cafile=str(args.ca))
    with (
        httpx.Client(base_url=origin, verify=tls, timeout=30) as operator,
        httpx.Client(base_url=origin, verify=tls, timeout=30) as viewer,
    ):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                if operator.get("/api/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        headers = {}
        for role, client in [("operator", operator), ("viewer", viewer)]:
            response = client.post(
                "/api/login",
                json={"username": "e02-" + role, "password": passwords[role]},
                headers={"Origin": origin},
            )
            assert response.status_code == 200, (response.status_code, response.text)
            headers[role] = {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}

        def inventory():
            return operator.get("/api/workloads").json()["items"]

        workloads = {r["name"]: r for r in inventory()}
        assert "e02-unlisted" not in workloads

        def path(name):
            return "/api/workloads/" + workloads[name]["id"]

        def candidate(name):
            response = operator.get(path(name) + "/baseline-candidate")
            assert response.status_code == 200, response.text
            value = response.json()
            assert (
                "private-literal-e02" not in response.text
                and "private-config-e02" not in response.text
            )
            return {"candidate_id": value["id"], "digest": value["digest"]}

        def confirm(name, body):
            return operator.post(path(name) + "/baseline", json=body, headers=headers["operator"])

        def context(name, baseline=None, client=operator):
            return client.get(
                path(name) + "/recovery-context",
                params={"baseline_id": baseline} if baseline else {},
            ).json()

        baseline_ids = {}
        initial_domain = domain()
        for name in ["e02-ready", "e02-recreate", "e02-config"]:
            body = candidate(name)
            time.sleep(5.3)
            response = confirm(name, body)
            assert response.status_code == 201, response.text
            saved = response.json()
            baseline_ids[name] = saved["id"]
            selected = context(name, saved["id"])
            assert (
                stable(selected["baseline"]) == stable(saved) and not selected["execution_eligible"]
            )
        assert domain() == initial_domain
        report["checks"]["healthy_baselines_before_faults_and_exact_actual_consumer"] = True
        # Generation change with exactly the same baseline envelope must invalidate pending confirmation.
        body = candidate("e02-ready")
        manifest = json.loads((folder / "e02-ready.json").read_text())
        changed = json.loads(json.dumps(manifest))
        changed["spec"]["template"]["metadata"]["annotations"] = {"probe": "generation-change"}
        kube("apply", "-f", "-", body=changed)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        time.sleep(5.3)
        assert confirm("e02-ready", body).status_code == 409
        report["checks"]["generation_change_rejects_pending"] = True
        kube("apply", "-f", "-", body=manifest)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        # Real fault: use a valid cached image with a crashing command, not registry/network failure.
        broken = json.loads(json.dumps(manifest))
        broken["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "python",
            "-c",
            "raise SystemExit(1)",
        ]
        kube("apply", "-f", "-", body=broken)
        deadline = time.monotonic() + 100
        fault = None
        while time.monotonic() < deadline:
            read = live_read(workloads["e02-ready"]["id"])
            if read.get("observation") and read["observation"]["health"] == "degraded":
                fault = read
                break
            time.sleep(1)
        assert fault is not None
        assert operator.get(path("e02-ready") + "/baseline-candidate").status_code == 409
        assert context("e02-ready")["reason"] == "missing"
        result = context("e02-ready", baseline_ids["e02-ready"])
        assert result["reason"] == "no_supported_diff" and not result["execution_eligible"]
        report["checks"]["actual_crash_refuses_baseline_and_has_manual_guidance"] = True
        report["fault"] = {
            "health": fault["observation"]["health"],
            "coverage": fault["observation"]["coverage"],
            "reasons": fault["observation"]["reasons"],
        }
        kube("apply", "-f", "-", body=manifest)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        assert live_read(workloads["e02-ready"]["id"])["observation"]["health"] == "healthy"
        # Reference identities change independently of image/resource fields; content stays unverified.
        config_manifest = json.loads((folder / "e02-config.json").read_text())
        kube(
            "apply",
            "-f",
            "-",
            body={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "e02-settings-new",
                    "namespace": "demo",
                    "labels": {"cloudops-probe": "e02-baselines"},
                },
                "data": {"MESSAGE": "private-config-e02"},
            },
        )
        body = candidate("e02-config")
        changed = json.loads(json.dumps(config_manifest))
        changed["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"configMapRef": {"name": "e02-settings-new"}}
        ]
        kube("apply", "-f", "-", body=changed)
        kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
        time.sleep(5.3)
        assert confirm("e02-config", body).status_code == 409
        result = context("e02-config", baseline_ids["e02-config"])
        assert result["reason"] == "config_unverified" and not result["execution_eligible"]
        report["checks"]["reference_change_rejects_confirm_and_context_is_manual"] = True
        kube("apply", "-f", "-", body=config_manifest)
        kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
        # Scoped name replacement must never transfer historical identity or authority.
        old = workloads["e02-recreate"]
        old_path = path("e02-recreate")
        old_history = operator.get(old_path + "/baselines").json()["items"]
        body = candidate("e02-recreate")
        kube("delete", "deployment", "e02-recreate", "-n", "demo", "--wait=true", "--timeout=45s")
        sync()
        assert not operator.get(old_path).json()["active"]
        kube("apply", "-f", "-", body=json.loads((folder / "e02-recreate.json").read_text()))
        kube("rollout", "status", "deployment/e02-recreate", "-n", "demo", "--timeout=90s")
        sync()
        new = next(r for r in inventory() if r["name"] == "e02-recreate" and r["active"])
        assert old["id"] != new["id"] and old["uid"] != new["uid"]
        assert operator.get("/api/workloads/" + new["id"] + "/baselines").json()["items"] == []
        assert (
            operator.post(
                "/api/workloads/" + new["id"] + "/baseline", json=body, headers=headers["operator"]
            ).status_code
            == 409
        )
        assert [stable(r) for r in operator.get(old_path + "/baselines").json()["items"]] == [
            stable(r) for r in old_history
        ]
        assert (
            operator.get(
                old_path + "/recovery-context", params={"baseline_id": baseline_ids["e02-recreate"]}
            ).json()["state"]
            == "manual_only"
        )
        report["replacement"] = {
            "old_workload_id": old["id"],
            "new_workload_id": new["id"],
            "old_uid": old["uid"],
            "new_uid": new["uid"],
            "old_baseline_id": baseline_ids["e02-recreate"],
        }
        report["checks"][
            "replacement_no_baseline_or_candidate_inheritance_old_history_retained"
        ] = True
        workloads["e02-recreate"] = new
        # Denied read and wrong trusted binding remain explicit, without retiring prior admission.
        try:
            (folder / "client.json").write_bytes((folder / "denied.json").read_bytes())
            read = live_read(workloads["e02-ready"]["id"])
            assert read["outcome"] == "failed" and read["reason"] == "permission_denied"
            observation = operator.get(path("e02-ready") + "/observation").json()
            assert (
                observation["summary"]["effective_health"] == "unknown"
                and observation["historical"]
            )
            assert context("e02-ready", baseline_ids["e02-ready"])["reason"] == "unavailable"
            before_inventory = {(r["id"], r["active"]) for r in inventory()}
            sync()
            assert {(r["id"], r["active"]) for r in inventory()} == before_inventory
            wrong = json.loads(policy)
            wrong["cluster"]["expected_fingerprint"] = "0" * 64
            (folder / "workloads.json").write_text(json.dumps(wrong))
            result = subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-m",
                    "cloudops.entrypoints.cli",
                    "sync-workloads",
                ],
                capture_output=True,
                text=True,
                env=os.environ,
            )
            assert result.returncode != 0
            assert {(r["id"], r["active"]) for r in inventory()} == before_inventory
            report["checks"]["denied_and_wrong_pin_unknown_without_retirement"] = True
        finally:
            (folder / "client.json").write_bytes(reader)
            (folder / "workloads.json").write_bytes(policy)
        sync()
        assert live_read(workloads["e02-ready"]["id"])["observation"]["health"] == "healthy"
        # Stop observer and age naturally; stale is not a database timestamp edit.
        command([*compose, "stop", "observer"])
        live_read(workloads["e02-ready"]["id"])
        time.sleep(61)
        stale = operator.get(path("e02-ready") + "/observation").json()
        assert (
            stale["summary"]["freshness"] == "stale"
            and stale["summary"]["effective_health"] == "unknown"
        )
        report["checks"]["natural_age_stale_without_fake_zero"] = True
        live_read(workloads["e02-ready"]["id"])
        assert (
            operator.get(path("e02-ready") + "/observation").json()["summary"]["effective_health"]
            == "healthy"
        )
        command([*compose, "start", "observer"])
        # Viewer denial is measured on complete protected application rows.
        before = domain()
        response = viewer.post(
            "/api/incidents",
            json={
                "workload_id": workloads["e02-ready"]["id"],
                "title": "Denied E02 Viewer",
                "description": "Controlled permission request",
            },
            headers={**headers["viewer"], "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 403 and domain() == before
        report["checks"]["viewer_investigation_denied_domain_unchanged"] = True
        # Upgrade is repeatable and stop/start retains both v1 and populated legacy data.
        legacy = app(
            "import json; from sqlalchemy import text; from cloudops.shared.database import sessions;\nwith sessions()() as db: print(json.dumps([dict(r) for r in db.execute(text(\"SELECT b.id::text,b.digest,b.snapshot,b.confirmed_by::text,b.confirmed_at::text FROM baselines b JOIN workloads w ON w.id=b.workload_id WHERE w.name='legacy-e02-fixture'\")).mappings()]))"
        )
        app(
            "from alembic import command; from alembic.config import Config; command.upgrade(Config('services/backend/alembic.ini'),'head'); command.upgrade(Config('services/backend/alembic.ini'),'head')"
        )
        before_selected = stable(context("e02-ready", baseline_ids["e02-ready"])["baseline"])
        command([*compose, "stop", "api", "worker", "observer"])
        command([*compose, "start", "api", "worker", "observer"])
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                if operator.get("/api/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        assert (
            stable(context("e02-ready", baseline_ids["e02-ready"])["baseline"]) == before_selected
        )
        after_legacy = app(
            "import json; from sqlalchemy import text; from cloudops.shared.database import sessions;\nwith sessions()() as db: print(json.dumps([dict(r) for r in db.execute(text(\"SELECT b.id::text,b.digest,b.snapshot,b.confirmed_by::text,b.confirmed_at::text FROM baselines b JOIN workloads w ON w.id=b.workload_id WHERE w.name='legacy-e02-fixture'\")).mappings()]))"
        )
        assert after_legacy == legacy
        report["checks"]["repeat_upgrade_stop_start_legacy_and_exact_selected_v1_preserved"] = True
    assert protected() == before_protected
    report["checks"]["original_checkout_uid_spec_and_writer_allowlist_unchanged"] = True
    report["baselines"] = baseline_ids
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def cleanup(_args):
    expected = json.loads((folder / "protected.json").read_text())
    assert protected() == expected, (
        "Protected checkout or writer policy changed; inspect before cleanup."
    )
    rows = json.loads(
        kube("get", "deployments", "-n", "demo", "-l", "cloudops-probe=e02-baselines", "-o", "json")
    )["items"]
    allowed = {"e02-ready", "e02-recreate", "e02-config", "e02-unlisted"}
    assert all(row["metadata"]["name"] in allowed for row in rows)
    for row in rows:
        kube(
            "delete",
            "deployment",
            row["metadata"]["name"],
            "-n",
            "demo",
            "--wait=true",
            "--timeout=45s",
        )
    refs = json.loads(
        kube("get", "configmaps", "-n", "demo", "-l", "cloudops-probe=e02-baselines", "-o", "json")
    )["items"]
    assert all(row["metadata"]["name"] in {"e02-settings", "e02-settings-new"} for row in refs)
    for row in refs:
        kube("delete", "configmap", row["metadata"]["name"], "-n", "demo")
    sync()
    assert protected() == expected
    print("Only owned E02 fixture resources removed; database and authority volumes retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="Create labeled fixtures and a separate application database/overlay."
    )
    prep.add_argument("--backend-image", default="cloudops-local-backend")
    prep.add_argument("--https-image", default="cloudops-local-https")
    probe = sub.add_parser(
        "verify", help="Exercise healthy baselines before faults and verify exact history/context."
    )
    probe.add_argument("--ca", type=Path, default=folder / "root.crt")
    probe.add_argument("--output", type=Path, default=folder / "integrated-api.json")
    probe.add_argument("--password-stdin", action="store_true")
    sub.add_parser(
        "cleanup", help="Remove only the fixed labeled fixtures; retain all records/volumes."
    )
    args = parser.parse_args()
    if not os.environ.get("POSTGRES_PASSWORD"):
        parser.error("Set POSTGRES_PASSWORD for the existing disposable local lab.")
    {"prepare": prepare, "verify": verify, "cleanup": cleanup}[args.command](args)


if __name__ == "__main__":
    main()
