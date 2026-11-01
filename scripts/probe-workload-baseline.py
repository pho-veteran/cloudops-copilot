"""Verify healthy v1 preparation and actual recovery context in the fixed HTTPS lab.

Creates only baseline/candidate/observation records. Kubernetes inspection is GET-only;
the selected Deployment must already be configured and healthy. No plan is proposed.
"""

import argparse
from getpass import getpass
import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time

import httpx
from local_lab import ROOT


def immutable(value):
    return {key: item for key, item in value.items() if key != "age_seconds"}


def deployment(workload):
    raw = subprocess.check_output(
        [
            "docker",
            "exec",
            "cloudops-e01-https-k3s-1",
            "/bin/kubectl",
            "get",
            "deployment",
            workload["name"],
            "-n",
            workload["namespace"],
            "-o",
            "json",
        ],
        text=True,
    )
    value = json.loads(raw)
    assert value["metadata"]["uid"] == workload["uid"], (
        "Live UID changed; inspect a fresh identity."
    )
    return {
        "uid": value["metadata"]["uid"],
        "generation": value["metadata"]["generation"],
        "resource_version": value["metadata"]["resourceVersion"],
        "spec_sha256": hashlib.sha256(
            json.dumps(value["spec"], sort_keys=True).encode()
        ).hexdigest(),
    }


def domain(client, workload_id):
    rows, offset = [], 0
    while True:
        page = client.get(
            "/api/incidents", params={"workload_id": workload_id, "offset": offset, "limit": 25}
        )
        assert page.status_code == 200, page.text
        rows.extend(page.json()["items"])
        offset = page.json()["next_offset"]
        if offset is None:
            break
    return {
        row["id"]: {
            "incident": row,
            "plans": client.get(f"/api/incidents/{row['id']}/plans").json(),
            "executions": client.get(f"/api/incidents/{row['id']}/executions").json(),
        }
        for row in rows
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--viewer", default="viewer")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/baseline28-live.json")
    args = parser.parse_args()
    passwords = (
        [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
        if args.password_stdin
        else [getpass("Operator password: "), getpass("Viewer password: ")]
    )
    origin = "https://localhost:8443"
    tls = ssl.create_default_context(cafile=str(args.ca))
    report = {
        "runtime_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "environment": "Actual local K3s, PostgreSQL and verified HTTPS API; browser proof recorded separately.",
        "checks": {},
    }
    with (
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=25) as operator,
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=25) as viewer,
    ):
        headers = {}
        for role, client, username, password in [
            ("operator", operator, args.operator, passwords[0]),
            ("viewer", viewer, args.viewer, passwords[1]),
        ]:
            login = client.post(
                "/api/login",
                json={"username": username, "password": password},
                headers={"Origin": origin},
            )
            assert login.status_code == 200, "Account sign-in failed."
            headers[role] = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        path = f"/api/workloads/{args.workload_id}"
        response = operator.get(path)
        assert response.status_code == 200, response.text
        workload = response.json()
        assert workload["active"] and workload["source"] == "live_kubernetes"
        before = deployment(workload)
        before_domain = domain(viewer, args.workload_id)
        missing = operator.get(path + "/recovery-context").json()
        assert missing["state"] == "manual_only" and missing["reason"] == "missing"
        candidate = operator.get(path + "/baseline-candidate")
        assert candidate.status_code == 200, candidate.text
        value = candidate.json()
        assert "_session_binding" not in candidate.text
        assert value["snapshot"]["profile"] == "deployment-images-resources/v1"
        body = {"candidate_id": value["id"], "digest": value["digest"]}
        history = viewer.get(path + "/baselines").json()
        young = operator.post(path + "/baseline", json=body, headers=headers["operator"])
        assert young.status_code == 409 and young.json()["error"]["code"] == "candidate_changed"
        assert (
            viewer.post(path + "/baseline", json=body, headers=headers["viewer"]).status_code == 403
        )
        assert [immutable(row) for row in viewer.get(path + "/baselines").json()["items"]] == [
            immutable(row) for row in history["items"]
        ]
        time.sleep(5.3)
        response = operator.post(path + "/baseline", json=body, headers=headers["operator"])
        assert response.status_code == 201, response.text
        saved = response.json()
        repeated = operator.post(path + "/baseline", json=body, headers=headers["operator"])
        assert repeated.status_code == 201 and immutable(repeated.json()) == immutable(saved)
        for client in (operator, viewer):
            response = client.get(path + "/recovery-context", params={"baseline_id": saved["id"]})
            assert response.status_code == 200, response.text
            context = response.json()
            assert (
                context["reason"] == "no_supported_diff" and context["execution_eligible"] is False
            )
            assert immutable(context["baseline"]) == immutable(saved)
        assert deployment(workload) == before, (
            "Target changed during proof; do not claim a zero-write receipt."
        )
        assert domain(viewer, args.workload_id) == before_domain
        report["checks"] = {
            key: True
            for key in (
                "no_selection_missing",
                "healthy_two_direct_reads_confirmed",
                "too_young_rejected",
                "viewer_denied_history_unchanged",
                "duplicate_preserves_original",
                "actual_selected_recovery_route_both_roles",
                "target_uid_spec_generation_resource_version_unchanged",
                "related_incidents_plans_executions_unchanged",
            )
        }
        report["target"] = {"workload_id": args.workload_id, **before}
        report["baseline"] = {
            key: saved[key]
            for key in ("id", "workload_id", "profile", "digest", "confirmed_at", "confirmed_by")
        }
        report["baseline"]["observations"] = [
            saved["snapshot"]["provenance"][key]["observation_id"] for key in ("first", "second")
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
