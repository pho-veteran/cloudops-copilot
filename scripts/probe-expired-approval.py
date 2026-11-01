"""Expire a real HTTPS approval session; requires fresh sign-in, plan read and explicit action.

Temporarily configures four-second sessions only in the named disposable local lab.
Restores normal API configuration even on failure. Never retries a mutation automatically.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

import httpx

from local_lab import COMPOSE


def configure(seconds):
    subprocess.run(
        [*COMPOSE, "up", "-d", "--no-deps", "--force-recreate", "api"],
        env={**os.environ, "SESSION_SECONDS": str(seconds)},
        check=True,
        capture_output=True,
        timeout=60,
    )


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
    tls = ssl.create_default_context(cafile="tmp/local-https-root.crt")
    iid = json.loads(Path("tmp/e01-consumer-smoke.json").read_text())["incident_id"]

    def wait(client):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if client.get("/api/health/ready").status_code == 200:
                return
            time.sleep(0.3)
        raise AssertionError("Local API did not become ready")

    def login(client):
        response = client.post(
            "/api/login",
            json={"username": "operator", "password": password},
            headers={"Origin": origin},
        )
        assert response.status_code == 200
        return response.json(), {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}

    configure(4)
    try:
        with httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15) as client:
            wait(client)
            session, headers = login(client)
            wid = json.loads(Path("tmp/e01-consumer-smoke.json").read_text())["workload_id"]
            candidate = client.get(f"/api/workloads/{wid}/baseline-candidate")
            assert candidate.status_code == 200
            replicas = (
                2
                if candidate.json()["snapshot"]["provenance"]["first"]["deployment"]["desired"] == 1
                else 1
            )
            before = client.get(f"/api/incidents/{iid}/executions").json()
            proposed = client.post(
                f"/api/incidents/{iid}/plans",
                json={
                    "replicas": replicas,
                    "rationale": "Real local session-expiry acceptance; explicit scale after fresh review",
                },
                headers=headers,
            )
            assert proposed.status_code == 201, proposed.text
            plan = proposed.json()
            path = f"/api/plans/{plan['id']}"
            review = client.get(path).json()
            assert review["valid"]
            body = {
                "version": plan["version"],
                "digest": plan["digest"],
                "review_token": review["review_token"],
                "decision": "approve",
            }
            cookie = client.cookies["cloudops_session"]
            expires = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
            delay = (expires - datetime.now(timezone.utc)).total_seconds() + 0.3
            assert 0 < delay < 5
            time.sleep(delay)
            expired = client.post(
                path + "/decision",
                json=body,
                headers={**headers, "Cookie": "cloudops_session=" + cookie},
            )
            assert expired.status_code == 401
            _, fresh_headers = login(client)
            old_review = client.post(path + "/decision", json=body, headers=fresh_headers)
            assert (
                old_review.status_code == 409
                and old_review.json()["error"]["code"] == "plan_refetch_required"
            )
            assert client.get(f"/api/incidents/{iid}/executions").json() == before
            fresh = client.get(path).json()
            assert fresh["valid"] and fresh["review_token"] != body["review_token"]
            body["review_token"] = fresh["review_token"]
            approved = client.post(path + "/decision", json=body, headers=fresh_headers)
            assert approved.status_code == 200, approved.text
            duplicate = client.post(path + "/decision", json=body, headers=fresh_headers)
            assert duplicate.status_code == 409
    finally:
        configure(28800)
    with httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15) as client:
        wait(client)
        login(client)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            executions = client.get(f"/api/incidents/{iid}/executions").json()
            current = next(e for e in executions if e["plan_id"] == plan["id"])
            if current["state"] not in ("queued", "executing"):
                break
            time.sleep(0.5)
        assert (
            len(executions) == len(before) + 1 and current["result"]["outcome"] == "acknowledged"
        ), current
        assert current["result"]["verification"] == "inconclusive"
    Path("tmp/e01-expired-approval.json").write_text(
        json.dumps(
            {
                "incident_id": iid,
                "plan_id": plan["id"],
                "execution_id": current["id"],
                "decision_statuses": [
                    expired.status_code,
                    old_review.status_code,
                    approved.status_code,
                    duplicate.status_code,
                ],
                "fresh_review_required": True,
                "one_execution_only": True,
                "outcome": current["result"],
            },
            indent=2,
        )
    )
    print(
        "PASS: real four-second session expired at the server; old intent denied, old review denied after sign-in, fresh read plus explicit approval admitted one write, duplicate denied. Normal session configuration restored."
    )


if __name__ == "__main__":
    main()
