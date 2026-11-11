"""Read-only five-pack E04 acceptance probe over an owned K3s + Prometheus lab."""

import argparse
import hashlib
import json
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from local_lab import COMPOSE, PROJECT, ROOT

PACKS = {
    "checkout-crashloop": "crashloop_startup",
    "checkout-oom": "oom_restarts",
    "checkout": "traffic_latency_errors",
    "checkout-pending": "pending_unschedulable",
    "checkout-imagepull": "image_pull",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


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
        "/api/login",
        json={"username": username, "password": password},
        headers={"Origin": origin},
    )
    response.raise_for_status()
    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}


def workloads(http, headers):
    page = http.get("/api/workloads", headers=headers).json()["items"]
    return {row["name"]: row for row in page if row["active"]}


def create_run(http, headers, workload, title):
    end = datetime.now(timezone.utc)
    response = http.post(
        "/api/incidents",
        json={
            "workload_id": workload["id"],
            "title": title,
            "description": "E04 integrated acceptance; not an RCA or recovery claim.",
            "window": {
                "start": (end - timedelta(minutes=1)).isoformat(),
                "end": end.isoformat(),
            },
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    response.raise_for_status()
    return response.json()


def wait_run(http, ids, headers):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        response = http.get(
            f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}", headers=headers
        )
        response.raise_for_status()
        run = response.json()
        if run["state"] in {"insufficient_evidence", "failed", "cancelled"}:
            return run
        time.sleep(2)
    raise RuntimeError("The Run did not reach a terminal state.")


def evidence(http, ids, run, headers):
    rows = {}
    for row in run["evidence"]:
        entry = {
            "source_key": row["source_key"],
            "id": row["id"],
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
            entry["payload_sha256"] = sha(payload)
            assert entry["payload_sha256"] == row["sha256"]
            if row["source_key"].startswith("prometheus."):
                entry["series"] = len(payload.get("series", []))
                entry["samples"] = sum(
                    len(series.get("samples", [])) for series in payload.get("series", [])
                )
            else:
                entry["payload_kind"] = content.json()["payload_kind"]
        rows[row["source_key"]] = entry
    return rows


def pack_status(run, pack):
    return next(row for row in run["pack_coverage"] if row["pack"] == pack)


def warm_metrics(ca, workload):
    """Wait for a complete minute in actual Prometheus, not a guessed sleep."""
    query = (
        "sum by (namespace, deployment_uid) (cloudops_requests_per_second{"
        f'namespace="{workload["namespace"]}",deployment_uid="{workload["uid"]}"' + "})"
    )
    deadline = time.monotonic() + 180
    with client("https://localhost:8443", ca) as prometheus:
        while time.monotonic() < deadline:
            end = time.time()
            response = prometheus.get(
                "/prometheus/api/v1/query_range",
                params={
                    "query": query,
                    "start": end - 60,
                    "end": end,
                    "step": 30,
                },
            )
            response.raise_for_status()
            series = response.json().get("data", {}).get("result", [])
            if series and all(len(row.get("values", [])) >= 3 for row in series):
                return
            time.sleep(2)
    raise RuntimeError("Prometheus has not retained a full one-minute metric window.")


def detail(http, headers, left, right):
    params = {
        "right_incident_id": right["incident_id"],
        "right_run_id": right["run_id"],
        "source_key": "kubernetes.state",
        "catalog_version": "standard_v1",
        "limit": 100,
    }
    rows = []
    for _ in range(20):
        response = http.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/compare",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        page = response.json()
        rows.extend(page["items"])
        if not page["next_cursor"]:
            return rows
        params["cursor"] = page["next_cursor"]
    raise RuntimeError("State comparison exceeded the acceptance page bound.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://localhost:8443")
    parser.add_argument("--ca", type=Path, default=ROOT / "tmp/e01-integration/prometheus/ca.crt")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/e04-integration-receipt.json")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if args.password_stdin:
        passwords = {role: sys.stdin.readline().rstrip("\r\n") for role in ("operator", "viewer")}
    else:
        passwords = {role: input(f"{role} password: ") for role in ("operator", "viewer")}
    report = {
        "schema": "E04IntegratedAcceptance/v1",
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "origin": args.origin,
        "proof_classes": {
            "producers": "Actual configured live_kubernetes and prometheus providers against the owned K3s/Prometheus lab.",
            "consumers": "Actual API, PostgreSQL, worker, stored evidence, timeline and comparison routes.",
            "browser": "Separate Edge receipt; the API receipt does not claim browser pixels.",
        },
        "packs": {},
    }
    images = json.loads(
        subprocess.run(
            [*COMPOSE, "images", "--format", "json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    report["runtime_images"] = [
        row for row in images if row["ContainerName"].startswith(PROJECT + "-")
    ]
    with client(args.origin, args.ca) as operator, client(args.origin, args.ca) as viewer:
        ready(operator)
        operator_headers = sign_in(operator, args.origin, "operator", passwords["operator"])
        viewer_headers = sign_in(viewer, args.origin, "viewer", passwords["viewer"])
        available = workloads(operator, operator_headers)
        warm_metrics(args.ca, available["checkout"])
        runs = {}
        for name, pack in PACKS.items():
            workload = available.get(name)
            if workload is None:
                raise RuntimeError(f"Configured workload {name} is missing.")
            ids = create_run(operator, operator_headers, workload, f"E04 {pack}")
            run = wait_run(operator, ids, operator_headers)
            rows = evidence(operator, ids, run, operator_headers)
            if run["state"] != "insufficient_evidence":
                raise RuntimeError(f"{name} Run ended {run['state']}.")
            if len(rows) != 12:
                raise RuntimeError(f"{name} published {len(rows)} sources, expected 12.")
            status = pack_status(run, pack)
            if status["status"] == "unavailable":
                raise RuntimeError(f"{pack} is unavailable.")
            if pack == "traffic_latency_errors" and status["status"] != "available":
                raise RuntimeError(f"{pack} is {status['status']}, expected available metrics.")
            entry = {
                "incident_id": ids["incident_id"],
                "run_id": ids["run_id"],
                "state": run["state"],
                "outcome_code": run["outcome_code"],
                "window": run["window"],
                "pack": status,
                "sources": rows,
            }
            runs[name] = entry
            report["packs"][pack] = entry

        checkout = available["checkout"]
        first_ids = runs["checkout"]
        lifecycle = f"/api/incidents/{first_ids['incident_id']}/lifecycle"
        operator.post(
            lifecycle,
            headers=operator_headers,
            json={
                "action": "resolve",
                "reason": "Freeze E04 evidence before the revision change",
            },
        ).raise_for_status()
        activity = operator.get(f"/api/incidents/{first_ids['incident_id']}/activity").json()[
            "items"
        ]
        resolution = next(row["id"] for row in activity if row["kind"] == "resolve")
        export_path = (
            f"/api/incidents/{first_ids['incident_id']}/handoff?resolution_id={resolution}"
        )
        baseline_export = operator.get(export_path)
        baseline_export.raise_for_status()
        operator.post(
            lifecycle,
            headers=operator_headers,
            json={
                "action": "reopen",
                "reason": "Observe a controlled template/revision change",
            },
        ).raise_for_status()
        # Only the bootstrap-owned local Deployment is mutated; never host kubectl.
        subprocess.run(
            [
                *COMPOSE,
                "exec",
                "-T",
                "k3s",
                "/bin/kubectl",
                "set",
                "env",
                "deployment/checkout",
                "-n",
                "demo",
                f"CLOUDOPS_E04_REVISION={uuid4()}",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                *COMPOSE,
                "exec",
                "-T",
                "k3s",
                "/bin/kubectl",
                "rollout",
                "status",
                "deployment/checkout",
                "-n",
                "demo",
                "--timeout=180s",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        end = datetime.now(timezone.utc)
        response = operator.post(
            f"/api/incidents/{first_ids['incident_id']}/runs",
            headers={**operator_headers, "Idempotency-Key": str(uuid4())},
            json={
                "workload_id": checkout["id"],
                "reason": "Compare the changed template revision",
                "window": {
                    "start": (end - timedelta(minutes=1)).isoformat(),
                    "end": end.isoformat(),
                },
            },
        )
        response.raise_for_status()
        second_ids = response.json()
        second_run = wait_run(operator, second_ids, operator_headers)
        second_rows = evidence(operator, second_ids, second_run, operator_headers)
        if len(second_rows) != 12:
            raise RuntimeError("The second traffic Run did not publish 12 sources.")
        comparison = operator.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}/compare",
            params={
                "right_incident_id": second_ids["incident_id"],
                "right_run_id": second_ids["run_id"],
            },
            headers=operator_headers,
        )
        comparison.raise_for_status()
        compare_body = comparison.json()
        if (
            compare_body["identity_relation"] != "same_identity"
            or len(compare_body["sources"]) != 12
        ):
            raise RuntimeError(
                "The two traffic Runs did not compare as the same 12-source identity."
            )
        report["comparison"] = {
            "left_incident": first_ids["incident_id"],
            "right_incident": second_ids["incident_id"],
            "left_run": runs["checkout"]["run_id"],
            "right_run": second_ids["run_id"],
            "status": compare_body["status"],
            "sources": len(compare_body["sources"]),
        }
        forward = detail(operator, operator_headers, first_ids, second_ids)
        assert forward == detail(operator, operator_headers, first_ids, second_ids)
        revision = next(row for row in forward if row["label"] == "deployment/revision")
        assert revision["status"] == "changed" and revision["left"] != revision["right"]
        reverse = {
            row["key"]: row for row in detail(operator, operator_headers, second_ids, first_ids)
        }
        for row in forward:
            other = reverse[row["key"]]
            assert other["left"] == row["right"] and other["right"] == row["left"]
            assert other["status"] == {"added": "removed", "removed": "added"}.get(
                row["status"], row["status"]
            )
        for side, ids in (("left", first_ids), ("right", second_ids)):
            ref = revision[f"{side}_citation"]
            exact = operator.get(
                f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/evidence/{ref['evidence_id']}/citation",
                params={
                    "locator_kind": ref["locator"]["kind"],
                    "locator_key": ref["locator"]["key"],
                },
            )
            exact.raise_for_status()
            assert exact.json()["target"]["value"] == revision[side]
        report["comparison"]["semantic_state"] = {
            "rows": len(forward),
            "revision": revision,
            "repeat_identical": True,
            "swap_reversed": True,
            "same_workload_uid": checkout["uid"],
        }
        assert operator.get(export_path).content == baseline_export.content
        report["saved_export"] = {
            "incident_id": first_ids["incident_id"],
            "resolution_id": resolution,
            "sha256": hashlib.sha256(baseline_export.content).hexdigest(),
            "bytes": len(baseline_export.content),
            "unchanged_after_reopen_revision_compare": True,
        }

        traffic_evidence = runs["checkout"]["sources"]
        request_rate = traffic_evidence["prometheus.request_rate"]
        targets = operator.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}"
            f"/evidence/{request_rate['id']}/citation-targets",
            headers=operator_headers,
        )
        targets.raise_for_status()
        sample = next(row for row in targets.json()["items"] if row["locator"]["kind"] == "sample")
        resolved = operator.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}"
            f"/evidence/{request_rate['id']}/citation",
            params={
                "locator_kind": sample["locator"]["kind"],
                "locator_key": sample["locator"]["key"],
            },
            headers=operator_headers,
        )
        resolved.raise_for_status()
        report["citation"] = {
            "source_key": "prometheus.request_rate",
            "locator": sample["locator"],
            "sha256": request_rate["sha256"],
        }

        timeline = operator.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}/timeline",
            headers=operator_headers,
        )
        timeline.raise_for_status()
        timeline_body = timeline.json()
        assert timeline_body["markers"][0]["kind"] == "window_start"
        assert timeline_body["markers"][-1]["kind"] == "window_end"
        report["timeline"] = {
            "schema": timeline_body["schema"],
            "kinds": [marker["kind"] for marker in timeline_body["markers"]],
            "truncated": timeline_body["truncated"],
        }

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Read consumers must not call a provider.")

        viewer_run = viewer.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}",
            headers=viewer_headers,
        )
        assert viewer_run.status_code == 200
        viewer_timeline = viewer.get(
            f"/api/incidents/{runs['checkout']['incident_id']}/runs/{runs['checkout']['run_id']}/timeline",
            headers=viewer_headers,
        )
        assert viewer_timeline.status_code == 200
        wrong = operator.get(
            f"/api/incidents/{uuid4()}/runs/{runs['checkout']['run_id']}/timeline",
            headers=operator_headers,
        )
        assert wrong.status_code == 404
        report["viewer_and_scope"] = {
            "viewer_run": viewer_run.status_code,
            "viewer_timeline": viewer_timeline.status_code,
            "wrong_scope": wrong.status_code,
        }

        before = {
            name: {
                "incident_id": row["incident_id"],
                "run_id": row["run_id"],
                "sources": {
                    key: (value["id"], value["sha256"]) for key, value in row["sources"].items()
                },
            }
            for name, row in report["packs"].items()
        }
        if args.restart:
            subprocess.run(
                [*COMPOSE, "restart", "api", "worker"], check=True, cwd=ROOT, capture_output=True
            )
            ready(operator)
            after = {}
            for name, row in report["packs"].items():
                run = operator.get(
                    f"/api/incidents/{row['incident_id']}/runs/{row['run_id']}",
                    headers=operator_headers,
                ).json()
                rows = evidence(
                    operator,
                    {"incident_id": row["incident_id"], "run_id": row["run_id"]},
                    run,
                    operator_headers,
                )
                after[name] = {
                    "incident_id": row["incident_id"],
                    "run_id": row["run_id"],
                    "sources": {key: (value["id"], value["sha256"]) for key, value in rows.items()},
                }
            report["restart"] = {"before": before, "after": after, "matches": before == after}
            if before != after:
                raise RuntimeError("Restart changed stored evidence IDs or hashes.")
            saved = operator.get(export_path)
            saved.raise_for_status()
            assert saved.content == baseline_export.content
            report["saved_export"]["unchanged_after_restart"] = True

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "packs": sorted(report["packs"]),
                "comparison": report["comparison"]["status"],
                "restart_matches": report.get("restart", {}).get("matches"),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
