"""Exercise checkpoints and trusted job dispatch against a disposable local Compose project.

Start with a fresh project (no permit yet), for example:

    CLOUDOPS_LAB_PROJECT=cloudops-issue46 docker compose -p cloudops-issue46 -f compose.yaml \
        up -d --build --wait api worker web
    uv run --project services/backend python scripts/probe-job-dispatch.py

Only the explicitly synthetic development fixture adapter is installed, so a lab kill can only
happen before its single read is checkpointed; resumption after a committed checkpoint is
proven by the PostgreSQL process tests. Prints and stores no secrets.
"""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROJECT = os.environ.get("CLOUDOPS_LAB_PROJECT", "cloudops-issue46")
LAB = ["docker", "compose", "-p", PROJECT, "-f", "compose.yaml"]
ORIGIN = "http://localhost:5173"
API = "http://127.0.0.1:8000"


def compose(*args, body=None, check=True, env=None):
    result = subprocess.run(
        [*LAB, *args],
        input=body,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode:
        raise RuntimeError(f"Lab command failed: {' '.join(args[:4])}: {result.stderr[-400:]}")
    return result


ADMIN = f"{PROJECT}-dispatch-admin-probe"


def admin(*args, check=True):
    """Run one trusted permit operation in a long-lived admin container (fast, deterministic)."""
    justification = ["--reason", "Issue 46 local acceptance", "--evidence", "probe-job-dispatch"]
    extra = justification if args[0] != "status" else []
    result = subprocess.run(
        ["docker", "exec", ADMIN, "python", "-m", "cloudops.entrypoints.dispatch_admin"]
        + [*args, *extra],
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise RuntimeError(f"Dispatch admin failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr.strip()}


def start_admin():
    subprocess.run(["docker", "rm", "-f", ADMIN], capture_output=True)
    compose("run", "-d", "--name", ADMIN, "--entrypoint", "sleep", "dispatch-admin", "infinity")


def worker(delay):
    compose(
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "worker",
        env={"COLLECTOR_DELAY_SECONDS": str(delay)},
    )


class Session:
    def __init__(self, username, password):
        self.http = httpx.Client(base_url=API, headers={"Origin": ORIGIN}, timeout=20)
        response = self.http.post("/api/login", json={"username": username, "password": password})
        response.raise_for_status()
        self.http.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    def get(self, path):
        return self.http.get(path)

    def post(self, path, body=None, key=False):
        headers = {"Idempotency-Key": str(uuid4())} if key else {}
        return self.http.post(path, json=body or {}, headers=headers)


def run_view(session, ids):
    response = session.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}")
    response.raise_for_status()
    return response.json()


def summary(view):
    return {
        "state": view["state"],
        "outcome_code": view["outcome_code"],
        "reason": view["reason"],
        "dispatch": view["dispatch"],
        "cancel": view["cancel"],
        "progress": view["progress"],
        "attempts": [
            {k: a[k] for k in ("number", "state", "error", "sources_committed")}
            for a in view["attempts"]
        ],
        "evidence": sorted((e["source_key"], e["id"]) for e in view["evidence"]),
    }


def wait(session, ids, states, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        view = run_view(session, ids)
        if view["state"] in states:
            return view
        time.sleep(0.5)
    raise AssertionError(f"Run did not reach {states}; last state {view['state']}.")


def rerun(session, incident, workload, reason):
    response = session.post(
        f"/api/incidents/{incident}/runs",
        {"workload_id": workload, "reason": reason, "window": {"preset": 15}},
        key=True,
    )
    assert response.status_code == 201, response.text
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/job-dispatch/api.json")
    args = parser.parse_args()
    checks = {}
    password = secrets.token_urlsafe(24)
    for name, role in (("issue46-operator", "operator"), ("issue46-viewer", "viewer")):
        compose(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "cloudops.entrypoints.cli",
            "account",
            name,
            "--role",
            role,
            "--password-stdin",
            body=password + "\n",
        )
    operator = Session("issue46-operator", password)
    viewer = Session("issue46-viewer", password)
    worker(0)

    status = admin("status")
    assert status["permit"] is None, "Start from a project without a dispatch permit."
    checks["fresh_status"] = {k: status[k] for k in ("problem", "denial", "audit")}
    workload = operator.get("/api/workloads").json()["items"][0]["id"]
    created = operator.post(
        "/api/incidents",
        {
            "workload_id": workload,
            "title": "Issue 46 dispatch probe",
            "description": "Kiểm tra checkpoint và permit.",
            "question": "Does collection wait for a trusted permit?",
        },
        key=True,
    )
    assert created.status_code == 201, created.text
    ids = created.json()
    incident = ids["incident_id"]
    time.sleep(6)
    view = run_view(operator, ids)
    assert view["state"] == "queued" and view["attempts"] == []
    assert view["dispatch"] == {"status": "paused", "reason": "permit_missing"}
    checks["missing_permit_denies_claim"] = summary(view)

    bootstrap = admin("bootstrap", "--fresh-install")
    first = bootstrap["permit"]
    assert (first["generation"], first["mode"]) == (1, "normal")
    checks["bootstrap"] = {
        "permit": first,
        "repeat": admin("bootstrap", "--fresh-install", check=False),
    }
    view = wait(operator, ids, {"insufficient_evidence"})
    assert view["progress"]["finalized"]
    assert len(view["evidence"]) == view["progress"]["sources_total"] == 12
    checks["collection_after_bootstrap"] = summary(view)

    recovery = admin("enter-recovery")["permit"]
    assert recovery["generation"] == 2 and recovery["allowed_job_kinds"] == []
    paused = rerun(operator, incident, workload, "Paused by recovery")
    time.sleep(6)
    view = run_view(operator, paused)
    assert view["state"] == "queued" and view["attempts"] == []
    assert view["dispatch"] == {"status": "paused", "reason": "recovery_mode"}
    assert view["cancel"] == {"allowed": True, "reason": "queued"}
    denied = viewer.post(f"/api/incidents/{incident}/runs/{paused['run_id']}/cancel")
    assert denied.status_code == 403
    checks["recovery_denies_claim"] = {
        "run": summary(view),
        "viewer_cancel_status": denied.status_code,
        "viewer_reads_same_boundary": run_view(viewer, paused)["cancel"] == view["cancel"],
    }
    resumed = admin("resume", "--class", "recommendation")["permit"]
    time.sleep(4)
    assert run_view(operator, paused)["state"] == "queued"
    resumed = admin("resume", "--class", "collection")["permit"]
    view = wait(operator, paused, {"insufficient_evidence"})
    checks["class_resumption"] = {
        "after_recommendation_only": "queued",
        "permit": resumed,
        "run": summary(view),
    }
    admin("exit-recovery")

    # Worker process loss during the fixture read; API restart reads persisted progress.
    worker(60)
    lost = rerun(operator, incident, workload, "Worker loss during collection")
    view = wait(operator, lost, {"collecting"})
    cancel = operator.post(f"/api/incidents/{incident}/runs/{lost['run_id']}/cancel")
    assert cancel.status_code == 409
    compose("kill", "-s", "SIGKILL", "worker")
    compose("restart", "api")
    compose("up", "-d", "--wait", "api")
    operator = Session("issue46-operator", password)
    during = run_view(operator, lost)
    assert during["state"] == "collecting"
    worker(0)
    view = wait(operator, lost, {"insufficient_evidence"}, seconds=120)
    assert [a["state"] for a in view["attempts"]] == ["expired", "succeeded"]
    checks["worker_loss_and_api_restart"] = {
        "cancel_while_collecting": cancel.json()["error"],
        "after_api_restart": summary(during),
        "final": summary(view),
    }

    # A generation change while a read is in flight: the late result cannot publish.
    worker(15)
    fenced = rerun(operator, incident, workload, "Generation change during collection")
    wait(operator, fenced, {"collecting"})
    admin("enter-recovery")
    time.sleep(18)
    stuck = run_view(operator, fenced)
    assert stuck["state"] == "collecting" and stuck["evidence"] == []
    logs = (
        compose("logs", "--no-log-prefix", "worker").stdout
        + compose("logs", "--no-log-prefix", "worker").stderr
    )
    assert "Obsolete job failure rejected" in logs or "stopped at a dispatch" in logs
    admin("exit-recovery")
    worker(0)
    view = wait(operator, fenced, {"insufficient_evidence"}, seconds=120)
    assert [a["state"] for a in view["attempts"]] == ["expired", "succeeded"]
    checks["late_result_fenced"] = {
        "during_recovery": summary(stuck),
        # The 15-second fixture exceeds its 5-second read deadline, so the attempt ends with a
        # failure whose publication is rejected under the retired generation.
        "worker_publication_rejected": "Obsolete job failure rejected" in logs
        or "stopped at a dispatch or lease boundary" in logs,
        "final": summary(view),
    }
    final = admin("status")
    checks["audit_operations"] = [entry["operation"] for entry in final["audit"]]
    checks["final_permit"] = final["permit"]
    result = {
        "source": "actual_local_compose_api_worker_postgresql_with_development_fixture_adapter",
        "project": PROJECT,
        "checks": checks,
        "limits": [
            "Only the synthetic development_fixture adapter is installed; the lab kill happens before its single read is checkpointed.",
            "Resumption after a committed checkpoint is proven by tests/test_collection_checkpoints.py with labeled test adapters and real worker processes.",
            "No E06 quota producer exists; waiting_for_quota cancellation is covered only by DB-seeded contract fixtures in tests.",
            "The executor is not part of this base stack; restored-approval refusal is covered by tests/test_dispatch_gate.py.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Job dispatch probe passed; receipt written to {args.output}.")


if __name__ == "__main__":
    start_admin()
    try:
        main()
    finally:
        subprocess.run(["docker", "rm", "-f", ADMIN], capture_output=True)
