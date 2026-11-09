"""Read-only receipt for the optional scoped Prometheus metric lab."""

import argparse
import json
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from local_lab import COMPOSE, ROOT

FOLDER = ROOT / "tmp/e01-integration/prometheus"
LAB = [*COMPOSE, "-f", str(ROOT / "compose.metrics.yaml")]
TERMINAL = {"insufficient_evidence", "failed", "cancelled"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def worktree():
    """Tracked-change summary; exclude session-scoped tool state from evidence."""
    return [
        line for line in git("status", "--porcelain=v1").splitlines() if ".commandcode/" not in line
    ]


def client(origin, ca):
    return httpx.Client(
        base_url=origin,
        verify=ssl.create_default_context(cafile=str(ca)),
        trust_env=False,
        timeout=30,
    )


def ready(http):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if http.get("/api/health/ready").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("The API did not become ready.")


def sign_in(http, origin, username, password):
    response = http.post(
        "/api/login", json={"username": username, "password": password}, headers={"Origin": origin}
    )
    response.raise_for_status()
    return {
        "Origin": origin,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def workload(http):
    page = http.get("/api/workloads").json()["items"]
    active = [row for row in page if row["active"]]
    chosen = next(
        (row for row in active if row["namespace"] == "demo" and row["name"] == "checkout"),
        active[0] if active else None,
    )
    if chosen is None:
        raise RuntimeError("No active configured workload is available.")
    return chosen


def wait_run(http, ids, headers):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        run = http.get(
            f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}", headers=headers
        ).json()
        if run["state"] in TERMINAL:
            return run
        time.sleep(2)
    raise RuntimeError("The investigation did not reach a terminal state.")


def evidence(http, ids, run, headers):
    rows = {}
    for row in run["evidence"]:
        if not row["source_key"].startswith("prometheus."):
            continue
        entry = {
            "id": row["id"],
            "source_key": row["source_key"],
            "coverage": row["coverage"],
            "content_status": row["content_status"],
            "sha256": row["sha256"],
            "query_metadata": row["query_metadata"],
        }
        if row["content_status"] == "available":
            content = http.get(
                f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}"
                f"/evidence/{row['id']}/content",
                headers=headers,
            )
            content.raise_for_status()
            payload = content.json()["payload"]
            entry["series"] = len(payload.get("series", []))
            entry["samples"] = sum(
                len(series.get("samples", [])) for series in payload.get("series", [])
            )
        rows[row["source_key"]] = entry
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://localhost:8443")
    parser.add_argument("--ca", type=Path, default=FOLDER / "ca.crt")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/prometheus48-receipt.json")
    parser.add_argument(
        "--expect", choices=["finite", "empty", "nonfinite", "gapped"], default="finite"
    )
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--window-minutes", type=int, choices=range(1, 61))
    args = parser.parse_args()
    passwords = (
        {role: sys.stdin.readline().rstrip("\r\n") for role in ("operator", "viewer")}
        if args.password_stdin
        else {role: input(f"{role} password: ") for role in ("operator", "viewer")}
    )
    report = {
        "runtime_commit": git("rev-parse", "HEAD"),
        "worktree": worktree(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "origin": args.origin,
        "expected": args.expect,
        "checks": {},
    }
    with client(args.origin, args.ca) as operator, client(args.origin, args.ca) as viewer:
        ready(operator)
        operator_headers = sign_in(operator, args.origin, "operator", passwords["operator"])
        viewer_headers = sign_in(viewer, args.origin, "viewer", passwords["viewer"])
        chosen = workload(operator)
        body = {
            "workload_id": chosen["id"],
            "title": "Prometheus metric evidence lab",
            "description": "Labeled local metric fixture; not a live app exporter or RCA.",
        }
        if args.window_minutes:
            end = datetime.now(timezone.utc)
            body["window"] = {
                "start": (end - timedelta(minutes=args.window_minutes)).isoformat(),
                "end": end.isoformat(),
            }
        response = operator.post(
            "/api/incidents",
            json=body,
            headers={**operator_headers, "Idempotency-Key": str(uuid4())},
        )
        response.raise_for_status()
        ids = response.json()
        report["ids"] = ids
        run = wait_run(operator, ids, operator_headers)
        report["run"] = {
            "state": run["state"],
            "outcome_code": run["outcome_code"],
            "window": run["window"],
            "pack_coverage": [
                {"pack": pack["pack"], "status": pack["status"]}
                for pack in run["pack_coverage"] or []
            ],
        }
        rows = evidence(operator, ids, run, operator_headers)
        report["evidence"] = rows
        report["checks"]["six_metric_sources_published"] = set(rows) == {
            f"prometheus.{name}"
            for name in (
                "request_rate",
                "error_ratio",
                "p95_latency",
                "memory_working_set",
                "memory_limit",
                "restarts",
            )
        }
        report["checks"]["analysis_remains_unavailable"] = (
            run["state"] == "insufficient_evidence"
            and run["outcome_code"] == "analysis_unavailable"
        )
        if args.expect == "finite":
            report["checks"]["all_six_available"] = len(rows) == 6 and all(
                row["coverage"]["status"] == "available" and row.get("series", 0) > 0
                for row in rows.values()
            )
        elif args.expect == "empty":
            report["checks"]["empty_states_are_explicit"] = all(
                row["coverage"]["status"] == "unavailable"
                and "no_samples" in row["coverage"]["reason_codes"]
                for row in rows.values()
            )
        elif args.expect == "nonfinite":
            report["checks"]["nonfinite_values_are_not_numeric"] = all(
                "no_samples" in row["coverage"]["reason_codes"] for row in rows.values()
            )
        else:
            report["checks"]["gaps_are_explicit"] = any(
                row["coverage"]["status"] in {"partial", "unavailable"} for row in rows.values()
            )
        timeline = operator.get(
            f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/timeline",
            headers=operator_headers,
        )
        timeline.raise_for_status()
        body = timeline.json()
        report["timeline"] = {
            "schema": body["schema"],
            "window": body["window"],
            "kinds": [marker["kind"] for marker in body["markers"]],
            "truncated": body["truncated"],
            "limitations": body["limitations"],
        }
        report["checks"]["timeline_has_captured_window"] = [
            marker["kind"] for marker in body["markers"][:1]
        ] == ["window_start"] and [marker["kind"] for marker in body["markers"][-1:]] == [
            "window_end"
        ]
        report["checks"]["viewer_reads_run"] = (
            viewer.get(
                f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}",
                headers=viewer_headers,
            ).status_code
            == 200
        )
        if args.restart:
            before = {key: (row["id"], row["sha256"]) for key, row in rows.items()}
            subprocess.run(
                [*LAB, "restart", "api", "worker"], check=True, cwd=ROOT, capture_output=True
            )
            ready(operator)
            after_run = wait_run(operator, ids, operator_headers)
            after = {
                key: (row["id"], row["sha256"])
                for key, row in evidence(operator, ids, after_run, operator_headers).items()
            }
            report["checks"]["restart_preserves_ids_and_hashes"] = before == after
            report["after_restart"] = after
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"receipt": str(args.output), "checks": report["checks"]}, indent=2))


if __name__ == "__main__":
    main()
