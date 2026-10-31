"""Exercise UID replacement in the fixed local lab; never recreates demo/checkout."""

import argparse
import copy
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import ssl
from local_lab import COMPOSE, ROOT

from cloudops.features.workloads.scope_config import cluster_fingerprint

NAMES = ("scope26-a", "scope26-b", "scope26-unlisted")
READER = ROOT / "tmp/e01-integration/reader"


def immutable_baseline(value):
    return {key: item for key, item in value.items() if key != "age_seconds"}


def command(*args, body=None, check=True):
    result = subprocess.run([*COMPOSE, *args], input=body, capture_output=True, text=True, cwd=ROOT)
    if check and result.returncode:
        raise RuntimeError("A local lab command failed; inspect its sanitized service status.")
    return result


def kube(*args, body=None):
    return command("exec", "-T", "k3s", "/bin/kubectl", *args, body=body).stdout


def write_policy(value):
    path = READER / "workloads.json"
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sync(expected=True):
    result = command(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        "sync-workloads",
        check=False,
    )
    assert (result.returncode == 0) == expected, "Unexpected scope synchronization outcome"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/scope26-live.json")
    parser.add_argument(
        "--other-reader",
        type=Path,
        help="Optional trusted reader for a separate empty lab cluster; never use a production connection.",
    )
    args = parser.parse_args()
    assert os.environ.get("POSTGRES_PASSWORD"), "Set the disposable lab PostgreSQL password."
    runtime_files = sorted((ROOT / "services/backend/src/cloudops").rglob("*.py"))
    source_hashes = {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in runtime_files
    }
    container_hashes = json.loads(
        command(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "import hashlib,json; from pathlib import Path; root=Path('/workspace'); print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'services/backend/src/cloudops').rglob('*.py'))}))",
        ).stdout
    )
    assert container_hashes == source_hashes, (
        "Rebuild the API from the current source before recording live evidence."
    )
    original = json.loads(kube("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    original_writer = (ROOT / "tmp/e01-integration/writer/workloads.json").read_bytes()
    existing = json.loads(kube("get", "deployments", "-n", "demo", "-o", "json"))
    assert not set(NAMES) & {r["metadata"]["name"] for r in existing["items"]}, (
        "Probe fixture names already exist; do not overwrite them."
    )
    assert not kube(
        "get", "serviceaccount", "scope26-denied", "-n", "demo", "--ignore-not-found"
    ).strip(), "Probe ServiceAccount already exists; do not overwrite it."
    reader = json.loads((READER / "client.json").read_text())
    fingerprint = cluster_fingerprint(reader)
    policy = {
        "schema_version": 2,
        "cluster": {
            "key": "local-k3s",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": fingerprint,
        },
        "deployments": [{"namespace": "demo", "name": name} for name in ("checkout", *NAMES[:2])],
    }
    password = secrets.token_urlsafe(24)
    for username, role in (("scope26-operator", "operator"), ("scope26-viewer", "viewer")):
        command(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "cloudops.entrypoints.cli",
            "account",
            username,
            "--role",
            role,
            "--password-stdin",
            body=password + "\n",
        )
    receipt = {
        "source": "live_local_k3s_https",
        "implementation_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "runtime_sources_sha256": hashlib.sha256(
            json.dumps(source_hashes, sort_keys=True).encode()
        ).hexdigest(),
        "cluster_fingerprint": fingerprint,
        "checks": {},
        "limits": [
            "Investigation collection is synthetic.",
            "This scope receipt does not validate E02 health or baseline v1.",
            "No executor was enabled by this probe.",
        ],
    }
    context = ssl.create_default_context(cafile=str(args.ca))
    try:
        for name in NAMES:
            fixture = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": "demo"},
                "spec": copy.deepcopy(original["spec"]),
            }
            fixture["spec"]["selector"]["matchLabels"] = {"app": name}
            fixture["spec"]["template"]["metadata"]["labels"] = {"app": name}
            kube("apply", "-f", "-", body=json.dumps(fixture))
            kube("rollout", "status", f"deployment/{name}", "-n", "demo", "--timeout=120s")
        write_policy(policy)
        sync()
        with httpx.Client(base_url="https://localhost:8443", verify=context, timeout=30) as api:
            login = api.post(
                "/api/login",
                json={"username": "scope26-operator", "password": password},
                headers={"Origin": "https://localhost:8443"},
            )
            assert login.status_code == 200
            headers = {
                "Origin": "https://localhost:8443",
                "X-CSRF-Token": login.json()["csrf_token"],
            }
            workloads = api.get("/api/workloads").json()["items"]
            assert {w["name"] for w in workloads} == {"checkout", *NAMES[:2]}
            old = next(w for w in workloads if w["name"] == NAMES[0])
            receipt["checks"]["allowlisted_only"] = True
            before = api.get("/api/workloads/scope").json()
            policy["cluster"]["expected_fingerprint"] = "0" * 64
            write_policy(policy)
            assert "Reader server/CA does not match" in sync(False).stderr
            assert api.get("/api/workloads/scope").json() == before
            receipt["checks"]["wrong_fingerprint_no_publication"] = True
            policy["cluster"]["expected_fingerprint"] = fingerprint
            write_policy(policy)
            if args.other_reader:
                other = json.loads(args.other_reader.read_text())
                assert cluster_fingerprint(other) != fingerprint
                before_reader = (READER / "client.json").read_bytes()
                try:
                    (READER / "client.json").write_text(json.dumps(other))
                    # Establish a real readable but empty alternate cluster with its own pin.
                    check = command(
                        "exec",
                        "-T",
                        "api",
                        "python",
                        "-c",
                        "import json,time; from pathlib import Path; from cloudops.features.workloads.scope_config import LiveScope,ScopeReader,Target,cluster_fingerprint; p=Path('/run/cloudops-reader/client.json'); c=json.loads(p.read_text()); s=LiveScope(schema_version=2,cluster={'key':'alternate-lab','fingerprint_algorithm':'server-ca-sha256/v1','expected_fingerprint':cluster_fingerprint(c)},deployments=[]); print(ScopeReader(s,p).lookup(Target(namespace='demo',name='checkout'),time.monotonic()+8).state)",
                    )
                    assert check.stdout.strip() == "missing"
                    assert "Reader server/CA does not match" in sync(False).stderr
                    assert api.get("/api/workloads/scope").json() == before
                    receipt["checks"]["other_readable_cluster_without_matching_names_rejected"] = (
                        True
                    )
                finally:
                    (READER / "client.json").write_bytes(before_reader)
            response = api.get(f"/api/workloads/{old['id']}/baseline-candidate")
            assert response.status_code == 200, (
                "Prepare the probe baseline only from a healthy fixture."
            )
            candidate = response.json()
            time.sleep(5.1)
            baseline_response = api.post(
                f"/api/workloads/{old['id']}/baseline",
                json={"candidate_id": candidate["id"], "digest": candidate["digest"]},
                headers=headers,
            )
            assert baseline_response.status_code == 201
            saved_baseline = immutable_baseline(baseline_response.json())
            admission = api.post(
                "/api/incidents",
                json={
                    "workload_id": old["id"],
                    "title": "Scope replacement live receipt",
                    "description": "Controlled disposable identity; investigation collection remains synthetic.",
                },
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            assert admission.status_code == 201
            saved_admission = admission.json()
            kube("delete", "deployment", NAMES[0], "-n", "demo", "--wait=true")
            sync()
            assert api.get(f"/api/workloads/{old['id']}").json()["retirement_reason"] == "deleted"
            fixture = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": NAMES[0], "namespace": "demo"},
                "spec": copy.deepcopy(original["spec"]),
            }
            fixture["spec"]["selector"]["matchLabels"] = {"app": NAMES[0]}
            fixture["spec"]["template"]["metadata"]["labels"] = {"app": NAMES[0]}
            kube("apply", "-f", "-", body=json.dumps(fixture))
            kube("rollout", "status", f"deployment/{NAMES[0]}", "-n", "demo", "--timeout=120s")
            sync()
            new = next(
                w for w in api.get("/api/workloads").json()["items"] if w["name"] == NAMES[0]
            )
            assert new["uid"] != old["uid"] and new["id"] != old["id"]
            assert api.get(f"/api/workloads/{new['id']}/baselines").json()["items"] == []
            assert [
                immutable_baseline(row)
                for row in api.get(f"/api/workloads/{old['id']}/baselines").json()["items"]
            ] == [saved_baseline]
            denied = api.post(
                "/api/incidents",
                json={
                    "workload_id": old["id"],
                    "title": "Must reject",
                    "description": "Retired identity",
                },
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            assert denied.status_code == 422
            assert (
                api.post(
                    f"/api/incidents/{saved_admission['incident_id']}/runs",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                ).status_code
                == 422
            )
            assert api.get(f"/api/incidents/{saved_admission['incident_id']}").status_code == 200
            receipt["checks"]["replacement_history_and_denied_old_admission"] = {
                "old_uuid": old["id"],
                "old_uid": old["uid"],
                "new_uuid": new["id"],
                "new_uid": new["uid"],
                "baseline_id": saved_baseline["id"],
                "incident_id": saved_admission["incident_id"],
            }
            sync()
            assert api.get(f"/api/workloads/{new['id']}").json()["active"]
            # Real 403 from a reader token without the demo reader RoleBinding.
            before_reader = (READER / "client.json").read_bytes()
            try:
                kube("create", "serviceaccount", "scope26-denied", "-n", "demo")
                denied_reader = {
                    **reader,
                    "token": kube(
                        "create", "token", "scope26-denied", "-n", "demo", "--duration=10m"
                    ).strip(),
                }
                (READER / "client.json").write_text(json.dumps(denied_reader))
                sync()
                denied_targets = api.get("/api/workloads/scope").json()["items"]
                assert all(
                    t["state"] == "unavailable" and t["reason"] == "permission_denied"
                    for t in denied_targets
                )
                assert {w["id"] for w in api.get("/api/workloads").json()["items"]} == {
                    w["id"] for w in workloads if w["id"] != old["id"]
                } | {new["id"]}
                receipt["checks"]["real_403_retains_admission"] = True
            finally:
                (READER / "client.json").write_bytes(before_reader)
                kube(
                    "delete", "serviceaccount", "scope26-denied", "-n", "demo", "--ignore-not-found"
                )
            sync()
            receipt["checks"]["read_recovery"] = all(
                t["state"] == "current" for t in api.get("/api/workloads/scope").json()["items"]
            )
            command("restart", "api", "worker")
            for _ in range(30):
                try:
                    if api.get("/api/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            assert [
                immutable_baseline(row)
                for row in api.get(f"/api/workloads/{old['id']}/baselines").json()["items"]
            ] == [saved_baseline]
            receipt["checks"]["restart_preserves_baseline"] = True
            api.post("/api/logout", headers=headers)
            viewer_login = api.post(
                "/api/login",
                json={"username": "scope26-viewer", "password": password},
                headers={"Origin": "https://localhost:8443"},
            )
            assert viewer_login.status_code == 200
            assert api.get("/api/workloads/scope").status_code == 200
            assert api.get(f"/api/workloads/{old['id']}").status_code == 200
            receipt["checks"]["viewer_history_and_scope"] = True
        # Kubernetes' authorization API proves the prepared reader cannot write.
        can_write = command(
            "exec",
            "-T",
            "k3s",
            "/bin/kubectl",
            "auth",
            "can-i",
            "patch",
            "deployments",
            "-n",
            "demo",
            "--as=system:serviceaccount:demo:cloudops-reader",
            check=False,
        )
        assert can_write.stdout.strip() == "no"
        receipt["checks"]["reader_patch_denied"] = True
    finally:
        for name in NAMES:
            kube("delete", "deployment", name, "-n", "demo", "--ignore-not-found")
        policy["cluster"]["expected_fingerprint"] = fingerprint
        policy["deployments"] = [{"namespace": "demo", "name": "checkout"}]
        write_policy(policy)
        sync()
        current = json.loads(kube("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
        assert (
            current["metadata"]["uid"] == original["metadata"]["uid"]
            and current["spec"] == original["spec"]
        )
        assert (ROOT / "tmp/e01-integration/writer/workloads.json").read_bytes() == original_writer
    receipt["checks"]["original_checkout_and_writer_allowlist_unchanged"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        "Live scope checks passed; sanitized receipt saved. Disposable Deployments removed; original checkout and writer authority preserved."
    )


if __name__ == "__main__":
    main()
