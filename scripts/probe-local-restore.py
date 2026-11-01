"""Restore the disposable cloudops-e01-https database behind independently closed authority.

Requires the live late-writer probe first, configured lab passwords on stdin, and Docker.
Stops/restarts this named lab's API/worker. Never use against a non-disposable database.
"""

import argparse
import json
import ssl
import subprocess
import sys
import time
from getpass import getpass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from local_lab import COMPOSE


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=90)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    operator_password, viewer_password = (
        [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
        if args.password_stdin
        else [getpass("Operator password: "), getpass("Viewer password: ")]
    )
    tls = ssl.create_default_context(cafile="tmp/local-https-root.crt")
    origin = "https://localhost:8443"
    late = json.loads(Path("tmp/e01-late-proof.json").read_text())
    uid = late["uid"]
    old = httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15)
    session = old.post(
        "/api/login",
        json={"username": "viewer", "password": viewer_password},
        headers={"Origin": origin},
    )
    assert session.status_code == 200
    cookie = old.cookies["cloudops_session"]
    old.close()
    backup = "/tmp/e01-restore-snapshot.dump"
    run(
        "docker",
        "exec",
        "cloudops-e01-https-db-1",
        "pg_dump",
        "-U",
        "cloudops",
        "-d",
        "cloudops",
        "-Fc",
        "-f",
        backup,
    )

    def maintain(operation):
        value = json.loads(
            run(
                "docker",
                "exec",
                "cloudops-e01-https-executor-1",
                "python",
                "-m",
                "cloudops.features.recovery.maintenance",
                operation,
                "--target",
                uid,
                "--reason",
                "Disposable database restore acceptance",
                "--evidence",
                "local://e01/pg-dump-restore",
            )
        )
        assert value["status"] == "applied"
        return value

    entered = maintain("enter_recovery")
    run("docker", "stop", "cloudops-e01-https-api-1", "cloudops-e01-https-worker-1")
    run(
        "docker",
        "exec",
        "cloudops-e01-https-db-1",
        "pg_restore",
        "--clean",
        "--if-exists",
        "-U",
        "cloudops",
        "-d",
        "cloudops",
        backup,
    )
    run(
        *COMPOSE,
        "run",
        "--rm",
        "--no-deps",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        "revoke-all-sessions",
    )
    run(*COMPOSE, "up", "-d", "--no-deps", "api", "executor", "krr")
    with httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15) as client:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if client.get("/api/health/ready").status_code == 200:
                break
            time.sleep(0.5)
        assert (
            client.get("/api/session", headers={"Cookie": "cloudops_session=" + cookie}).status_code
            == 401
        ), "Restored browser cookie was not revoked"
        session = client.post(
            "/api/login",
            json={"username": "operator", "password": operator_password},
            headers={"Origin": origin},
        )
        assert session.status_code == 200
        headers = {"Origin": origin, "X-CSRF-Token": session.json()["csrf_token"]}
        status = json.loads(
            run(
                "docker",
                "exec",
                "cloudops-e01-https-executor-1",
                "python",
                "-m",
                "cloudops.features.recovery.maintenance",
                "status",
            )
        )
        assert all(t["recovery_mode"] and not t["enabled"] for t in status["targets"])
        assert any(
            a["id"] == late["execution_id"] and a["state"] == "outcome_unknown"
            for a in status["unknown_attributions"]
        )
        iid = json.loads(Path("tmp/e01-consumer-smoke.json").read_text())["incident_id"]
        admitted = late.get("execution_created_at")
        if admitted:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(admitted)).total_seconds()
            time.sleep(max(0, 31 - age))
        wid = json.loads(Path("tmp/e01-consumer-smoke.json").read_text())["workload_id"]
        candidate = client.get(f"/api/workloads/{wid}/baseline-candidate")
        assert candidate.status_code == 200
        replicas = (
            2
            if candidate.json()["snapshot"]["provenance"]["first"]["deployment"]["desired"] == 1
            else 1
        )
        path = f"/api/incidents/{iid}/plans"
        body = {
            "replicas": replicas,
            "rationale": "Fresh explicit proposal after controlled database restore",
        }
        before = client.get(f"/api/incidents/{iid}/executions").json()
        denied = client.post(path, json=body, headers=headers)
        assert (
            denied.status_code == 409 and denied.json()["error"]["code"] == "target_quarantined"
        ), denied.text
        assert client.get(f"/api/incidents/{iid}/executions").json() == before
        enabled, exited = maintain("enable_target"), maintain("exit_recovery_mode")
        proposed = client.post(path, json=body, headers=headers)
        assert proposed.status_code == 201, proposed.text
        plan = proposed.json()
        review = client.get(f"/api/plans/{plan['id']}").json()
        assert review["valid"]
        rejected = client.post(
            f"/api/plans/{plan['id']}/decision",
            json={
                "version": plan["version"],
                "digest": plan["digest"],
                "review_token": review["review_token"],
                "decision": "reject",
            },
            headers=headers,
        )
        assert rejected.status_code == 200
    run(*COMPOSE, "up", "-d", "--no-deps", "worker")
    output = {
        "restored_database": "disposable cloudops-e01-https/cloudops",
        "old_cookie_denied": True,
        "restored_unlocked_application_state_did_not_enable_gateway": True,
        "historical_unknown_retained": late["execution_id"],
        "enter": entered,
        "enable": enabled,
        "exit": exited,
        "fresh_proposal_then_explicit_reject": plan["id"],
    }
    Path("tmp/e01-restore-proof.json").write_text(json.dumps(output, indent=2))
    run("docker", "exec", "cloudops-e01-https-db-1", "rm", "--", backup)
    print(
        "PASS: actual pg_dump/pg_restore retained independent quarantine/unknown attribution; restored browser sessions were revoked through the CLI before API startup; fresh proof and explicit enable/exit were required."
    )


if __name__ == "__main__":
    main()
