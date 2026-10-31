"""Drive the real late-writer probe through root CLI and HTTPS approval in the disposable lab."""

import argparse
import json
import queue
import ssl
import subprocess
import sys
import threading
import time
from getpass import getpass
from pathlib import Path
from uuid import uuid4

import httpx

from local_lab import COMPOSE, ROOT


def run(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, cwd=ROOT, timeout=60
    ).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass("Operator password: ")
    )
    origin = "https://localhost:8443"
    run(*COMPOSE, "stop", "worker")
    run(
        "docker",
        "cp",
        "scripts/probe-late-writer.py",
        "cloudops-e01-https-executor-1:/tmp/probe-late-writer.py",
    )
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "cloudops-e01-https-executor-1",
            "python",
            "-u",
            "/tmp/probe-late-writer.py",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        assert lines.get(timeout=30).startswith("READY:"), (
            "Probe did not enter independent quarantine"
        )
        uid = json.loads(Path("tmp/e01-integration/writer/workloads.json").read_text())[0]["uid"]
        for operation in ("enable_target", "exit_recovery_mode"):
            value = json.loads(
                run(
                    *COMPOSE,
                    "exec",
                    "-T",
                    "executor",
                    "python",
                    "-m",
                    "cloudops.features.recovery.maintenance",
                    operation,
                    "--target",
                    uid,
                    "--reason",
                    "Controlled late-writer integration admission",
                    "--evidence",
                    "local://e01/late-writer",
                )
            )
            assert value["status"] == "applied"
        # Establish the feature admission cooldown before creating the new attempt.
        time.sleep(31)
        with httpx.Client(
            base_url=origin,
            verify=ssl.create_default_context(cafile="tmp/local-https-root.crt"),
            trust_env=False,
            timeout=20,
        ) as client:
            session = client.post(
                "/api/login",
                json={"username": "operator", "password": password},
                headers={"Origin": origin},
            )
            assert session.status_code == 200
            headers = {"Origin": origin, "X-CSRF-Token": session.json()["csrf_token"]}
            workload = next(
                w for w in client.get("/api/workloads").json()["items"] if w["uid"] == uid
            )
            preview = client.get(f"/api/workloads/{workload['id']}/baseline-candidate")
            assert preview.status_code == 200
            replicas = (
                2
                if preview.json()["snapshot"]["provenance"]["first"]["deployment"]["desired"] == 1
                else 1
            )
            response = client.post(
                "/api/incidents",
                json={
                    "workload_id": workload["id"],
                    "title": "Controlled live late-writer acceptance",
                    "description": "Explicit manual intent; the controlled network delivery delay tests real Kubernetes fencing, not synthetic diagnosis.",
                },
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 201
            iid = response.json()["incident_id"]
            response = client.post(
                f"/api/incidents/{iid}/plans",
                json={
                    "replicas": replicas,
                    "rationale": "Controlled delayed delivery after explicit HTTPS approval",
                },
                headers=headers,
            )
            assert response.status_code == 201, response.text
            plan = response.json()
            review = client.get(f"/api/plans/{plan['id']}").json()
            assert review["valid"]
            response = client.post(
                f"/api/plans/{plan['id']}/decision",
                json={
                    "version": plan["version"],
                    "digest": plan["digest"],
                    "review_token": review["review_token"],
                    "decision": "approve",
                },
                headers=headers,
            )
            assert response.status_code == 200
            execution = client.get(f"/api/incidents/{iid}/executions").json()[0]
            assert execution["state"] == "queued"
            Path("tmp/e01-late-request.json").write_text(
                json.dumps({"execution_id": execution["id"]})
            )
        run(
            "docker",
            "cp",
            "tmp/e01-late-request.json",
            "cloudops-e01-https-executor-1:/tmp/e01-late-request.json",
        )
        stdout, _ = process.communicate(timeout=60)
        assert process.returncode == 0 and "PASS:" in stdout, (
            "Live late-writer probe failed; preserve quarantine and inspect its sanitized outcome"
        )
        run(
            "docker",
            "cp",
            "cloudops-e01-https-executor-1:/tmp/e01-late-proof.json",
            "tmp/e01-late-proof.json",
        )
        run(*COMPOSE, "up", "-d", "--no-deps", "worker")
        print(stdout.strip())
    finally:
        if process.poll() is None:
            process.terminate()


if __name__ == "__main__":
    main()
