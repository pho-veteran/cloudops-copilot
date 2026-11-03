"""Drive a pinned real Alertmanager into local K3s/HTTPS intake; prints and stores no secrets."""

import argparse
import json
import os
import re
import secrets
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from local_lab import COMPOSE, ROOT

from cloudops.features.workloads.scope_config import cluster_fingerprint

LAB = [*COMPOSE, "-f", "compose.alertmanager.yaml"]
READER = ROOT / "tmp/e01-integration/reader"
ORIGIN = "https://localhost:8443"


def command(*args, body=None, check=True):
    result = subprocess.run([*LAB, *args], input=body, capture_output=True, text=True, cwd=ROOT)
    if check and result.returncode:
        raise RuntimeError(f"A local lab command failed: {' '.join(args[:3])}")
    return result


def cli(*args, body=None, check=True):
    return command(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        *args,
        body=body,
        check=check,
    )


def amtool(*args):
    command(
        "exec",
        "-T",
        "alertmanager",
        "amtool",
        "--alertmanager.url=http://localhost:9093",
        "alert",
        "add",
        *args,
    )


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/intake36-sender.json")
    args = parser.parse_args()
    assert os.environ.get("POSTGRES_PASSWORD"), "Set the disposable lab PostgreSQL password."
    reader = json.loads((READER / "client.json").read_text())
    policy = {
        "schema_version": 2,
        "cluster": {
            "key": "local-k3s",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": cluster_fingerprint(reader),
        },
        "deployments": [{"namespace": "demo", "name": "checkout"}],
    }
    path = READER / "workloads.json"
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    cli("sync-workloads")
    password = secrets.token_urlsafe(24)
    cli(
        "account",
        "intake36-operator",
        "--role",
        "operator",
        "--password-stdin",
        body=password + "\n",
    )
    version = command("exec", "-T", "alertmanager", "alertmanager", "--version").stdout
    version = version or command("exec", "-T", "alertmanager", "alertmanager", "--version").stderr
    pinned = re.search(r"prom/alertmanager:\S+", (ROOT / "compose.alertmanager.yaml").read_text())
    tag = uuid4().hex[:6]
    direct, pod_alert, scoped = (f"Intake36{kind}{tag}" for kind in ("Direct", "Pod", "Scope"))
    receipt = {
        "source": "live_local_k3s_https_alertmanager",
        "implementation_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "sender_image": pinned.group(0) if pinned else None,
        "sender_version": version.strip().splitlines()[0] if version.strip() else None,
        "checks": {},
        "steps": [],
        "limits": [
            "Investigation collection remains synthetic; no diagnosis or model call is made.",
            "One configured source and one pinned cluster; no rate limiting is claimed.",
            "Alerts are injected into the real Alertmanager with amtool; Prometheus rules are not part of this receipt.",
        ],
    }
    context = ssl.create_default_context(cafile=str(args.ca))
    with httpx.Client(base_url=ORIGIN, verify=context, timeout=30) as api:
        login = api.post(
            "/api/login",
            json={"username": "intake36-operator", "password": password},
            headers={"Origin": ORIGIN},
        )
        assert login.status_code == 200, "Lab sign-in failed"

        def receipts():
            response = api.get("/api/alert-receipts", params={"limit": 100})
            assert response.status_code == 200
            return response.json()

        def wait_for(name, predicate, timeout=120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for item in receipts()["items"]:
                    if predicate(item):
                        receipt["steps"].append(
                            {
                                "step": name,
                                "outcome": item["outcome"],
                                "reason": item["reason"],
                                "alert_status": item["alert_status"],
                                "incident_id": item["incident_id"],
                                "run_id": item["run_id"],
                                "received_at": item["received_at"],
                            }
                        )
                        return item
                time.sleep(2)
            raise AssertionError(f"No intake receipt for step {name}")

        page = receipts()
        assert page["configured"] and page["source_id"] == "lab-alertmanager"
        newest = page["items"][0]["id"] if page["items"] else None
        wrong = api.post(
            "/api/intake/alertmanager",
            content=json.dumps({"version": "4", "status": "firing", "alerts": []}),
            headers={
                "Authorization": "Bearer not-the-sender-token",
                "Content-Type": "application/json",
            },
        )
        assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "intake_unauthorized"
        after = receipts()["items"]
        assert (after[0]["id"] if after else None) == newest
        receipt["checks"]["wrong_credential_401_without_receipt"] = True

        first_start = datetime.now(timezone.utc) - timedelta(minutes=1)
        labels = [
            f"alertname={direct}",
            "namespace=demo",
            "deployment=checkout",
            "severity=critical",
        ]
        amtool(*labels, f"--start={rfc3339(first_start)}", "--annotation=summary=lab annotation")
        accepted = wait_for(
            "direct_firing",
            lambda r: r["alertname"] == direct and r["outcome"] in ("accepted", "deferred"),
        )
        incident = api.get(f"/api/incidents/{accepted['incident_id']}").json()
        assert incident["origin"] == {"kind": "alertmanager", "source_id": "lab-alertmanager"}
        assert incident["alert_signal"] == "firing" and incident["severity"]["value"] == "critical"
        assert "lab annotation" not in json.dumps(incident)
        receipt["checks"]["direct_deployment_uid_bound_incident"] = True
        wait_for("repeat", lambda r: r["alertname"] == direct and r["outcome"] == "duplicate")
        incidents = api.get("/api/incidents", params={"limit": 100}).json()["items"]
        assert sum(1 for i in incidents if i["title"].startswith(direct)) == 1
        receipt["checks"]["repeat_is_duplicate_without_new_incident"] = True

        pods = json.loads(
            command(
                "exec",
                "-T",
                "k3s",
                "/bin/kubectl",
                "get",
                "pods",
                "-n",
                "demo",
                "-l",
                "app=checkout",
                "-o",
                "json",
            ).stdout
        )["items"]
        pod = next(p["metadata"]["name"] for p in pods if p["status"].get("phase") == "Running")
        amtool(f"alertname={pod_alert}", "namespace=demo", f"pod={pod}")
        by_pod = wait_for(
            "pod_owner_chain",
            lambda r: r["alertname"] == pod_alert and r["outcome"] in ("accepted", "deferred"),
        )
        assert by_pod["incident_id"] != accepted["incident_id"]
        assert (
            api.get(f"/api/incidents/{by_pod['incident_id']}").json()["workload_id"]
            == incident["workload_id"]
        )
        receipt["checks"]["pod_owner_chain_maps_same_uid"] = True

        amtool(f"alertname={scoped}", "namespace=demo", "deployment=not-configured-intake36")
        wait_for(
            "out_of_scope",
            lambda r: r["alertname"] == scoped
            and r["outcome"] == "rejected"
            and r["reason"] == "out_of_scope",
        )
        receipt["checks"]["out_of_scope_rejected_without_incident"] = True

        amtool(
            *labels,
            f"--start={rfc3339(first_start)}",
            f"--end={rfc3339(datetime.now(timezone.utc))}",
        )
        wait_for(
            "resolved_signal",
            lambda r: r["alertname"] == direct and r["outcome"] == "resolved_signal",
        )
        resolved = api.get(f"/api/incidents/{accepted['incident_id']}").json()
        assert resolved["alert_signal"] == "resolved" and resolved["state"] == "open"
        receipt["checks"]["resolved_alert_is_signal_only"] = True

        amtool(*labels, f"--start={rfc3339(datetime.now(timezone.utc))}")
        wait_for(
            "new_episode",
            lambda r: r["alertname"] == direct
            and r["outcome"] in ("accepted", "deferred")
            and r["incident_id"] != accepted["incident_id"],
        )
        receipt["checks"]["new_firing_start_is_new_incident"] = True
        listing = json.dumps(receipts())
        token = (ROOT / "tmp/e01-integration/intake-sender/token").read_text().strip()
        assert token not in listing and "lab annotation" not in listing
        receipt["checks"]["receipts_redacted"] = True
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        f"Recorded {len(receipt['checks'])} sender checks in {args.output}; no secrets were printed."
    )


if __name__ == "__main__":
    main()
