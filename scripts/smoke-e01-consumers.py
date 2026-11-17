"""Exercise actual local E01 consumers over HTTPS; creates labeled application records."""

import argparse
import hashlib
import json
import ssl
import sys
import time
from getpass import getpass
from pathlib import Path
from uuid import uuid4

import httpx


def baseline_history(client, workload_id):
    page = client.get(f"/api/workloads/{workload_id}/baselines").json()
    return [
        {key: value for key, value in row.items() if key != "age_seconds"} for row in page["items"]
    ]


def verify_report_export(client, report):
    path = f"/api/recommendation-reports/{report['id']}/export"
    exported = client.get(path)
    assert exported.status_code == 200, exported.text
    dynamic = {
        "applicability",
        "dispatch_paused",
        "dispatch_reason",
        "export_ready",
        "export_sha256",
    }
    assert exported.json() == {key: value for key, value in report.items() if key not in dynamic}
    assert hashlib.sha256(exported.content).hexdigest() == report["export_sha256"]
    assert client.get(path).content == exported.content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--viewer", default="viewer")
    parser.add_argument(
        "--workload-id", help="Exact configured live workload for this scale probe."
    )
    args = parser.parse_args()
    passwords = (
        [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
        if args.password_stdin
        else [getpass("Operator password: "), getpass("Viewer password: ")]
    )
    origin = "https://localhost:8443"
    tls = ssl.create_default_context(cafile=args.ca)
    with (
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=20) as operator,
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=20) as viewer,
    ):

        def login(client, username, password):
            response = client.post(
                "/api/login",
                json={"username": username, "password": password},
                headers={"Origin": origin},
            )
            assert response.status_code == 200, "Account sign-in failed"
            return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}

        oh, vh = (
            login(operator, args.operator, passwords[0]),
            login(viewer, args.viewer, passwords[1]),
        )
        if args.workload_id:
            selected = operator.get(f"/api/workloads/{args.workload_id}")
            assert selected.status_code == 200, selected.text
            workload = selected.json()
            assert workload["active"] and workload["source"] == "live_kubernetes"
        else:
            workload = next(
                w
                for w in operator.get("/api/workloads").json()["items"]
                if w["source"] == "live_kubernetes"
            )
        wid = workload["id"]
        response = operator.post(
            "/api/incidents",
            json={
                "workload_id": wid,
                "title": "E01 live consumer acceptance",
                "description": "Local access acceptance. Investigation collector output remains explicitly synthetic; manual plans use fresh live target observations.",
            },
            headers={**oh, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        ids = response.json()
        iid = ids["incident_id"]
        candidate = operator.get(f"/api/workloads/{wid}/baseline-candidate")
        assert candidate.status_code == 200, candidate.text
        time.sleep(5.2)
        body = {"candidate_id": candidate.json()["id"], "digest": candidate.json()["digest"]}
        before = baseline_history(viewer, wid)
        assert (
            viewer.post(f"/api/workloads/{wid}/baseline", json=body, headers=vh).status_code == 403
        )
        assert baseline_history(viewer, wid) == before
        assert (
            operator.post(f"/api/workloads/{wid}/baseline", json=body, headers=oh).status_code
            == 201
        )
        print("Live baseline preview/confirmation and Viewer denial: passed.")
        assert (
            viewer.post(
                f"/api/incidents/{iid}/notes", json={"text": "Denied Viewer note"}, headers=vh
            ).status_code
            == 403
        )
        assert (
            operator.post(
                f"/api/incidents/{iid}/notes",
                json={"text": "Local live access verification"},
                headers=oh,
            ).status_code
            == 201
        )
        for action in ("acknowledge", "resolve"):
            body = {"action": action, "reason": "Local E01 lifecycle verification"}
            assert (
                viewer.post(f"/api/incidents/{iid}/lifecycle", json=body, headers=vh).status_code
                == 403
            )
            response = operator.post(f"/api/incidents/{iid}/lifecycle", json=body, headers=oh)
            assert response.status_code == 200, response.text
        resolution = next(
            a
            for a in viewer.get(f"/api/incidents/{iid}/activity").json()["items"]
            if a["kind"] == "resolve"
        )
        export_url = f"/api/incidents/{iid}/handoff?resolution_id={resolution['id']}"
        saved = viewer.get(export_url).content
        assert b"Local live access verification" in saved
        assert (
            operator.post(
                f"/api/incidents/{iid}/lifecycle",
                json={"action": "reopen", "reason": "Continue the access exercise"},
                headers=oh,
            ).status_code
            == 200
        )
        assert (
            operator.post(
                f"/api/incidents/{iid}/notes",
                json={"text": "New note after resolution"},
                headers=oh,
            ).status_code
            == 201
        )
        assert viewer.get(export_url).content == saved
        print("Notes, lifecycle, immutable resolution and Viewer handoff export: passed.")
        for component in ("cluster", "database", "worker"):
            assert (
                viewer.post(
                    "/api/connection-checks", json={"component": component}, headers=vh
                ).status_code
                == 403
            )
            response = operator.post(
                "/api/connection-checks", json={"component": component}, headers=oh
            )
            assert response.status_code == 201, response.text
            if component == "cluster":
                assert response.json()["status"] == "available", response.text
        assert viewer.get("/api/connection-checks").status_code == 200
        print("Actual scoped connection checks and role enforcement: passed.")
        body = {"workload_id": wid, "requested_hours": 6}
        assert viewer.post("/api/recommendation-reports", json=body, headers=vh).status_code == 403
        response = operator.post("/api/recommendation-reports", json=body, headers=oh)
        assert response.status_code == 201, response.text
        report_id = response.json()["id"]
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            report = viewer.get(f"/api/recommendation-reports/{report_id}").json()
            if report["state"] not in ("queued", "running"):
                break
            time.sleep(1)
        assert report["result"].get("source") == "live_krr", report
        assert report["result"].get("tool_exit_code") is not None, "KRR invocation did not finish"
        assert report["result"]["recommendations"] is None and report["result"]["coverage"] is None
        verify_report_export(viewer, report)
        print(
            f"Pinned KRR invocation and actual persisted Viewer export: {report['state']} (tool exit {report['result']['tool_exit_code']}); no qualified recommendations claimed."
        )
        desired = (
            2
            if candidate.json()["snapshot"]["provenance"]["first"]["deployment"]["desired"] == 1
            else 1
        )
        response = operator.post(
            f"/api/incidents/{iid}/plans",
            json={
                "replicas": desired,
                "rationale": "Bounded explicit local scale for E01 authorization proof",
            },
            headers=oh,
        )
        assert response.status_code == 201, response.text
        plan = response.json()
        assert (
            viewer.post(
                f"/api/incidents/{iid}/plans",
                json={"replicas": desired, "rationale": "Valid Viewer attempt"},
                headers=vh,
            ).status_code
            == 403
        )
        receipt = operator.get(f"/api/plans/{plan['id']}").json()
        body = {
            "version": plan["version"],
            "digest": plan["digest"],
            "review_token": receipt["review_token"],
            "decision": "approve",
        }
        assert (
            viewer.post(f"/api/plans/{plan['id']}/decision", json=body, headers=vh).status_code
            == 403
        )
        response = operator.post(f"/api/plans/{plan['id']}/decision", json=body, headers=oh)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            executions = viewer.get(f"/api/incidents/{iid}/executions").json()
            if executions and executions[0]["state"] not in ("queued", "executing"):
                break
            time.sleep(0.5)
        assert executions[0]["result"]["outcome"] == "acknowledged", executions
        assert (
            executions[0]["result"]["verification"] == "inconclusive"
            and not executions[0]["holds_lock"]
        )
        print(
            "Exact-plan approval, durable worker dispatch and real conditional Kubernetes write: acknowledged; application verification remains inconclusive."
        )
        output = {
            **ids,
            "workload_id": wid,
            "uid": workload["uid"],
            "report_id": report_id,
            "plan_id": plan["id"],
            "execution_id": executions[0]["id"],
            "report_receipt": {"state": report["state"], "result": report["result"]},
            "execution_outcome": executions[0]["result"],
            "baseline_candidate_digest": candidate.json()["digest"],
        }
        Path("tmp/e01-consumer-smoke.json").write_text(
            json.dumps(output, indent=2), encoding="utf-8"
        )
        print(
            "Sanitized identities saved to tmp/e01-consumer-smoke.json; no credential material exported."
        )


if __name__ == "__main__":
    main()
