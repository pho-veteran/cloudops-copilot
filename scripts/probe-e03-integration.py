"""Exercise real sender, intake and bounded dispatch in an isolated HTTPS lab.

This probe changes durable lab state; see docs/features/incidents/alert-intake.md before running it.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from local_lab import COMPOSE, PROJECT, ROOT

LAB = [*COMPOSE, "-f", "compose.alertmanager.yaml"]
ORIGIN = "https://localhost:8443"
TOKEN = ROOT / "tmp/e01-integration/intake-sender/token"


def command(*args, body=None):
    result = subprocess.run(
        [*LAB, *args], input=body, capture_output=True, text=True, cwd=ROOT, timeout=180
    )
    if result.returncode:
        raise RuntimeError(f"Lab command failed: {' '.join(args[:3])}")
    return result.stdout.strip()


def sql_uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Invalid UUID in probe query") from None


def sql_fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]+", value):
        raise RuntimeError("Invalid fingerprint in probe query")
    return value


def db(query):
    # Queries only read persistence; dynamic identifiers must be validated before interpolation.
    output = command(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "cloudops",
        "-d",
        "cloudops",
        "-At",
        "-c",
        query,
    )
    return json.loads(output)


def counts():
    return db(
        "SELECT json_build_object('episodes',(SELECT count(*) FROM alert_episodes),"
        "'receipts',(SELECT count(*) FROM alert_intake_receipts),"
        "'incidents',(SELECT count(*) FROM incidents),"
        "'runs',(SELECT count(*) FROM investigation_runs),"
        "'jobs',(SELECT count(*) FROM jobs))"
    )


def run_data(run_id):
    return db(
        "SELECT json_build_object('id',r.id,'state',r.state,'queue_reason',r.queue_reason,"
        "'created_at',r.created_at,'jobs',COALESCE((SELECT json_agg(json_build_object("
        "'id',j.id,'state',j.state,'attempts',j.attempts,'created_at',j.created_at) "
        "ORDER BY j.created_at,j.id) FROM jobs j WHERE j.kind='development_collection_v1' "
        f"AND j.subject_id=r.id),'[]'::json)) FROM investigation_runs r WHERE r.id='{sql_uuid(run_id)}'"
    )


def wait_for(description, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {description}")


def alert(name, fingerprint=None, start=None, deployment="checkout"):
    return {
        "status": "firing",
        "fingerprint": fingerprint or uuid4().hex,
        "startsAt": start or (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "labels": {"alertname": name, "namespace": "demo", "deployment": deployment},
        "annotations": {"summary": "e03-probe-private-annotation"},
    }


def sender_metric(port):
    program = (
        "import json,re,urllib.request; "
        f"s=urllib.request.build_opener(urllib.request.ProxyHandler({{}})).open('http://https:{port}/metrics',timeout=3).read().decode(); "
        "names=('alertmanager_notifications_total','alertmanager_notifications_failed_total'); "
        "found={n:[float(m.group(1)) for line in s.splitlines() "
        "if (m:=re.match(r'^'+n+r'\\{[^}]*integration=\"webhook\"[^}]*\\}\\s+(\\S+)',line))] "
        "for n in names}; print(json.dumps({n:sum(v) if v else None for n,v in found.items()}))"
    )
    try:
        return json.loads(command("exec", "-T", "api", "python", "-c", program))
    except (RuntimeError, json.JSONDecodeError):
        return None


def concurrent_senders(api, uid, cancel, receipt, owned_containers, owned_files):
    # One delivery per isolated Alertmanager: no peer clustering and no repeat within the probe.
    tag = uuid4().hex[:12]
    config_path = TOKEN.parent / f"e03-senders-{tag}.yml"
    with config_path.open("x", encoding="utf-8") as config_file:
        owned_files.append(config_path)
        config_file.write(
            "route:\n  receiver: cloudops\n  group_by: [alertname, namespace]\n"
            "  group_wait: 2s\n  group_interval: 1h\n  repeat_interval: 24h\n"
            "receivers:\n  - name: cloudops\n    webhook_configs:\n"
            "      - url: https://localhost:8443/api/intake/alertmanager\n"
            "        send_resolved: true\n        timeout: 5s\n"
            "        http_config:\n          authorization:\n            type: Bearer\n"
            "            credentials_file: /run/cloudops-intake/token\n"
            "          tls_config:\n            ca_file: /run/cloudops-intake/proxy-ca.crt\n"
        )
    ports = [20000 + secrets.randbelow(20000), 20000 + secrets.randbelow(20000)]
    while ports[1] == ports[0]:
        ports[1] = 20000 + secrets.randbelow(20000)
    for index, port in enumerate(ports):
        container = command(
            "run",
            "--no-deps",
            "-d",
            "--rm",
            "--name",
            f"{PROJECT}-e03-{tag}-{index}",
            "--entrypoint",
            "alertmanager",
            "alertmanager",
            f"--config.file=/run/cloudops-intake/{config_path.name}",
            f"--web.listen-address=0.0.0.0:{port}",
            "--cluster.listen-address=",
            f"--storage.path=/tmp/e03-{tag}-{index}",
        )
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise RuntimeError("Could not identify an isolated Alertmanager container")
        owned_containers.append(container)
        image_id = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            text=True,
            timeout=30,
        ).strip()
        if image_id != receipt["sender_image"]["image_id"]:
            raise RuntimeError("Isolated Alertmanager image differs from the pinned lab sender")
        wait_for(
            "isolated sender readiness",
            lambda port=port: sender_metric(port),
            timeout=60,
        )
    name = f"E03RealConcurrent{tag}"
    start = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt["steps"].append(
        {
            "step": "concurrent_sender_input",
            "labels": {"alertname": name, "namespace": "demo", "deployment": "checkout"},
            "starts_at": start,
            "sender_container_ids": list(owned_containers),
        }
    )

    def inject(index):
        result = subprocess.run(
            [
                "docker",
                "exec",
                owned_containers[index],
                "amtool",
                f"--alertmanager.url=http://localhost:{ports[index]}",
                "alert",
                "add",
                f"alertname={name}",
                "namespace=demo",
                "deployment=checkout",
                f"--start={start}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Could not inject the alert into an isolated pinned sender")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(inject, range(2)))

    def delivered():
        response = api.get("/api/alert-receipts", params={"limit": 100})
        if response.status_code != 200:
            raise RuntimeError("Cannot read concurrent sender receipts")
        rows = [row for row in response.json()["items"] if row["alertname"] == name]
        return rows if len(rows) == 2 and len({row["delivery_id"] for row in rows}) == 2 else None

    rows = wait_for("two isolated sender deliveries", delivered, timeout=90)
    if {row["outcome"] for row in rows} != {"accepted", "duplicate"} or len(
        {(row["fingerprint"], row["incident_id"], row["run_id"]) for row in rows}
    ) != 1:
        raise RuntimeError("Two sender deliveries did not deduplicate to one incident and Run")
    metrics = []
    for port in ports:

        def success(port=port):
            values = sender_metric(port)
            if (
                values
                and values["alertmanager_notifications_total"] is not None
                and values["alertmanager_notifications_total"] >= 1
            ):
                return values
            return None

        values = wait_for("sender webhook success metric", success, timeout=60)
        # Prometheus counter vectors have no series until their first increment.
        # Preserve None in the receipt rather than reporting a fabricated zero.
        if values["alertmanager_notifications_failed_total"] not in (None, 0):
            raise RuntimeError("An isolated sender reported a failed webhook notification")
        metrics.append(values)
    row = rows[0]
    persisted = db(
        "SELECT json_build_object('episodes',count(*),'uid',min(workload_uid),"
        "'incidents',count(DISTINCT incident_id),'runs',count(DISTINCT run_id),"
        f"'initial_runs',(SELECT count(*) FROM investigation_runs WHERE incident_id='{sql_uuid(row['incident_id'])}')) "
        f"FROM alert_episodes WHERE fingerprint='{sql_fingerprint(row['fingerprint'])}'"
    )
    if persisted != {
        "episodes": 1,
        "uid": uid,
        "incidents": 1,
        "runs": 1,
        "initial_runs": 1,
    }:
        raise RuntimeError("Concurrent sender deliveries did not persist one UID-bound initial Run")
    receipt["checks"]["two_real_concurrent_senders"] = {
        "sender_container_ids": list(owned_containers),
        "metrics": metrics,
        "deliveries": rows,
        "episode": persisted,
    }
    cancel(row["incident_id"], row["run_id"])


def replace_owned_checkout(api, send, first, original, receipt):
    metadata = original["metadata"]
    uid = metadata["uid"]
    writer = json.loads((ROOT / "tmp/e01-integration/writer/workloads.json").read_text())
    policy = json.loads((ROOT / "tmp/e01-integration/reader/workloads.json").read_text())
    expected = {
        "cluster": "local-k3s",
        "namespace": "demo",
        "name": "checkout",
        "uid": uid,
        "source": "live_kubernetes",
    }
    containers = original.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if (
        writer != [expected]
        or policy.get("schema_version") != 2
        or policy.get("cluster", {}).get("key") != "local-k3s"
        or policy.get("deployments") != [{"namespace": "demo", "name": "checkout"}]
        or metadata.get("name") != "checkout"
        or metadata.get("namespace") != "demo"
        or metadata.get("labels", {}).get("app") != "checkout"
        or metadata.get("ownerReferences")
        or metadata.get("deletionTimestamp")
        or len(containers) != 1
        or containers[0].get("name") != "checkout"
        or containers[0].get("image") != "python:3.13.11-slim-bookworm"
        or containers[0].get("command") != ["python", "-m", "http.server", "8080"]
    ):
        raise RuntimeError(
            "Replacement refused: Deployment, pinned writer, or v2 scope is not the owned demo lab"
        )
    current = json.loads(
        command(
            "exec",
            "-T",
            "k3s",
            "/bin/kubectl",
            "get",
            "deployment",
            "checkout",
            "-n",
            "demo",
            "-o",
            "json",
        )
    )
    if (
        current["metadata"]["uid"] != uid
        or current["spec"] != original["spec"]
        or current["metadata"].get("labels", {}).get("app") != "checkout"
        or current["metadata"].get("ownerReferences")
        or current["metadata"].get("deletionTimestamp")
    ):
        raise RuntimeError(
            "Replacement refused: demo Deployment identity or spec changed since inspection"
        )
    old = db(
        "SELECT json_build_object('id',e.id,'uid',e.workload_uid,'workload_id',e.workload_id,"
        "'incident_id',e.incident_id,'run_id',e.run_id,'fingerprint',e.fingerprint,"
        "'starts_at_key',e.starts_at_key) FROM alert_episodes e "
        f"WHERE e.incident_id='{sql_uuid(first['incident_id'])}'"
    )
    if old["uid"] != uid or old["run_id"] != first["run_id"]:
        raise RuntimeError("Replacement refused: original episode binding changed")

    def unknowns():
        return db(
            "SELECT COALESCE(json_agg(json_build_object('id',id,'plan_id',plan_id,"
            "'workload_id',workload_id,'state',state,'holds_lock',holds_lock,"
            "'maintenance_released',maintenance_released,'result_hash',md5(result::text)) "
            "ORDER BY id),'[]'::json) FROM recovery_executions "
            f"WHERE workload_id='{sql_uuid(old['workload_id'])}' AND state='outcome_unknown'"
        )

    retained_before = unknowns()
    if not any(row["holds_lock"] and not row["maintenance_released"] for row in retained_before):
        raise RuntimeError(
            "Replacement requires a retained old-UID unknown execution holding its lock"
        )
    # A fresh create contains only the live spec and safe metadata; no server identity,
    # owner references, Deployment or pod authority annotations, or status are copied.
    spec = current["spec"]
    spec["template"]["metadata"] = {
        "labels": spec["template"].get("metadata", {}).get("labels", {})
    }
    fresh = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout",
            "namespace": "demo",
            "labels": {"app": "checkout"},
        },
        "spec": spec,
    }
    receipt["steps"].append(
        {
            "step": "replacement_precondition",
            "old_episode": old,
            "retained_unknowns": retained_before,
        }
    )
    options = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {key: current["metadata"][key] for key in ("uid", "resourceVersion")},
    }
    command(
        "exec",
        "-T",
        "k3s",
        "/bin/kubectl",
        "delete",
        "--raw",
        "/apis/apps/v1/namespaces/demo/deployments/checkout",
        "-f",
        "-",
        body=json.dumps(options),
    )
    receipt["steps"].append({"step": "old_deployment_delete_requested", "uid": uid})

    def deleted():
        return not command(
            "exec",
            "-T",
            "k3s",
            "/bin/kubectl",
            "get",
            "deployment",
            "checkout",
            "-n",
            "demo",
            "--ignore-not-found",
            "-o",
            "name",
        )

    wait_for("old Deployment deletion", deleted, timeout=180)
    command("exec", "-T", "k3s", "/bin/kubectl", "create", "-f", "-", body=json.dumps(fresh))
    command(
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
    )
    replacement = json.loads(
        command(
            "exec",
            "-T",
            "k3s",
            "/bin/kubectl",
            "get",
            "deployment",
            "checkout",
            "-n",
            "demo",
            "-o",
            "json",
        )
    )
    new_uid = replacement["metadata"]["uid"]
    if new_uid == uid:
        raise RuntimeError("Replacement Deployment retained the old UID")
    receipt["steps"].append({"step": "replacement_created", "old_uid": uid, "new_uid": new_uid})
    command(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        "sync-workloads",
    )
    old_view = api.get(f"/api/incidents/{old['incident_id']}")
    if (
        old_view.status_code != 200
        or old_view.json()["workload"]["active"]
        or old_view.json()["workload"]["uid"] != uid
    ):
        raise RuntimeError("Old workload history was not preserved as inactive")
    entry = alert(first["alertname"], fingerprint=old["fingerprint"], start=old["starts_at_key"])
    response = send([entry])
    if response.status_code != 200 or response.json()["counts"].get("duplicate") != 1:
        raise RuntimeError("Old episode did not remain a duplicate after UID replacement")
    new_entry = dict(entry)
    new_entry["startsAt"] = datetime.now(timezone.utc).isoformat()
    response = send([new_entry])
    if response.status_code != 200 or response.json()["counts"].get("accepted") != 1:
        raise RuntimeError("New episode did not admit against the replacement Deployment")
    bindings = db(
        "SELECT COALESCE(json_agg(json_build_object('id',id,'uid',workload_uid,"
        "'workload_id',workload_id,'incident_id',incident_id,'run_id',run_id,"
        "'starts_at_key',starts_at_key) ORDER BY starts_at_key),'[]'::json) "
        f"FROM alert_episodes WHERE fingerprint='{sql_fingerprint(old['fingerprint'])}'"
    )
    preserved = [row for row in bindings if row["id"] == old["id"]]
    newer = [row for row in bindings if row["id"] != old["id"] and row["uid"] == new_uid]
    if (
        len(preserved) != 1
        or len(newer) != 1
        or preserved[0]["uid"] != uid
        or preserved[0]["incident_id"] != old["incident_id"]
        or preserved[0]["run_id"] != old["run_id"]
        or newer[0]["incident_id"] == old["incident_id"]
        or newer[0]["run_id"] == old["run_id"]
    ):
        raise RuntimeError("Old and replacement episode identities were not preserved separately")
    if unknowns() != retained_before:
        raise RuntimeError(
            "Retained unknown execution attribution or lock changed during replacement"
        )
    if json.loads((ROOT / "tmp/e01-integration/writer/workloads.json").read_text()) != writer:
        raise RuntimeError("Old-UID writer allowlist changed during replacement")
    receipt["checks"]["guarded_uid_replacement"] = {
        "old_uid": uid,
        "new_uid": new_uid,
        "bindings": bindings,
        "retained_unknowns": retained_before,
        "writer_allowlist": "unchanged; still pinned to old UID",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument(
        "--replace-owned-checkout",
        action="store_true",
        help="delete and recreate the disposable lab's UID-verified demo/checkout Deployment",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not os.environ.get("CLOUDOPS_LAB_PROJECT")
        or not re.fullmatch(r"cloudops-[a-z0-9][a-z0-9_-]*", PROJECT)
        or PROJECT in {"cloudops-local", "cloudops-e01-https"}
    ):
        raise RuntimeError(
            "Explicitly set CLOUDOPS_LAB_PROJECT to a disposable cloudops-* Compose project"
        )
    if not os.environ.get("POSTGRES_PASSWORD"):
        raise RuntimeError("Set POSTGRES_PASSWORD for the disposable lab")
    receipt = {
        "project": PROJECT,
        "checks": {},
        "steps": [],
        "status": "running",
        "limits": [
            "Collection is synthetic; no live AI or remediation is asserted.",
            "Direct v4 deliveries are labeled separately from the real Alertmanager sender.",
            "No fairness is claimed under continuous competing admissions.",
            "Lifecycle/claim/callback races require separate acceptance evidence.",
        ],
    }
    worker_stopped = False
    worker_initially_running = False
    owned_containers = []
    worker_restore_allowed = True
    owned_files = []
    try:
        receipt["runner_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
        ).strip()
        receipt["runner_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        images = command("images", "--format", "json")
        receipt["runtime_images"] = json.loads(images)
        sender_container = command("ps", "-q", "alertmanager")
        if len(sender_container.splitlines()) != 1:
            raise RuntimeError("Exactly one Alertmanager container is required")
        sender_image = (
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Config.Image}} {{.Image}}",
                    sender_container,
                ],
                text=True,
                timeout=30,
            )
            .strip()
            .split()
        )
        pinned_sender = "prom/alertmanager:v0.34.2@sha256:39d8bb3966c694a124428a3a88fbcffd40bd069129ecd47c059f8a3f17c55f3b"
        if len(sender_image) != 2 or sender_image[0] != pinned_sender:
            raise RuntimeError("Running Alertmanager is not the pinned lab sender")
        receipt["sender_image"] = {
            "reference": sender_image[0],
            "image_id": sender_image[1],
        }
        version = subprocess.run(
            [*LAB, "exec", "-T", "alertmanager", "alertmanager", "--version"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=30,
        )
        if version.returncode or not (version.stdout or version.stderr).strip():
            raise RuntimeError("Could not read the running Alertmanager version")
        receipt["sender_version"] = (version.stdout or version.stderr).strip().splitlines()[0]

        def config(service):
            return json.loads(
                command(
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    "import json; from cloudops.shared.config import settings; "
                    "from cloudops.features.investigations.admission_settings import deferred_run_limit; "
                    "print(json.dumps({'J':settings().queue_limit,'D':deferred_run_limit(),"
                    "'collector_delay_seconds':settings().collector_delay_seconds}))",
                )
            )

        limits = config("api")
        if config("worker") != limits or not (2 <= limits["J"] <= 100 and 2 <= limits["D"] <= 100):
            raise RuntimeError(
                "API/worker capacity must agree; this bounded probe requires 2..100 for J and D"
            )
        receipt["capacity"] = limits
        running = command("ps", "--status", "running", "--services").splitlines()
        if "worker" not in running:
            raise RuntimeError("Start exactly one disposable lab worker before invoking the probe")
        if len(command("ps", "-q", "worker").splitlines()) != 1:
            raise RuntimeError("The disposable project must have exactly one worker container")
        worker_initially_running = True
        deployment = json.loads(
            command(
                "exec",
                "-T",
                "k3s",
                "/bin/kubectl",
                "get",
                "deployment",
                "checkout",
                "-n",
                "demo",
                "-o",
                "json",
            )
        )
        uid = deployment["metadata"]["uid"]
        receipt["deployment"] = {
            "cluster": "local-k3s",
            "namespace": "demo",
            "name": "checkout",
            "uid": uid,
        }
        password = secrets.token_urlsafe(24)
        username = "e03probe-" + uuid4().hex[:12]
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
            "operator",
            "--password-stdin",
            body=password + "\n",
        )
        context = ssl.create_default_context(cafile=str(args.ca))
        with httpx.Client(base_url=ORIGIN, verify=context, timeout=30, trust_env=False) as api:
            login = api.post(
                "/api/login",
                json={"username": username, "password": password},
                headers={"Origin": ORIGIN},
            )
            if login.status_code != 200:
                raise RuntimeError("Disposable operator sign-in failed")
            headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
            token = TOKEN.read_text(encoding="utf-8").strip()
            machine = {"Authorization": f"Bearer {token}"}

            def send(entries, auth=machine):
                return api.post(
                    "/api/intake/alertmanager",
                    headers=auth,
                    json={"version": "4", "status": "firing", "alerts": entries},
                )

            def page():
                response = api.get("/api/alert-receipts", params={"limit": 100})
                if response.status_code != 200:
                    raise RuntimeError("Cannot read intake receipts")
                return response.json()["items"]

            def find(name, outcome):
                return next(
                    (
                        item
                        for item in page()
                        if item["alertname"] == name and item["outcome"] == outcome
                    ),
                    None,
                )

            def cancel(incident, run):
                response = api.post(
                    f"/api/incidents/{incident}/runs/{run}/cancel",
                    headers=headers,
                    json={"reason": "Disposable E03 capacity probe"},
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Could not cancel queued probe Run: HTTP {response.status_code}"
                    )

            # Stop the only project worker and require an otherwise empty active queue.
            worker_stopped = True
            command("stop", "worker")
            active = db(
                "SELECT json_build_object('jobs',(SELECT count(*) FROM jobs WHERE state IN ('queued','running')),"
                "'deferred',(SELECT count(*) FROM investigation_runs WHERE state='queued' AND queue_reason='capacity'))"
            )
            if active != {"jobs": 0, "deferred": 0}:
                raise RuntimeError("Disposable lab must have no active Jobs or deferred Runs")
            before = counts()
            wrong = send([alert("E03Wrong")], {"Authorization": "Bearer wrong-credential"})
            if wrong.status_code != 401 or counts() != before:
                raise RuntimeError(
                    "Wrong authentication wrote domain/receipt rows or was not rejected"
                )
            receipt["checks"]["wrong_auth_no_database_writes"] = {
                "http_status": wrong.status_code,
                "before": before,
            }
            concurrent_senders(api, uid, cancel, receipt, owned_containers, owned_files)
            # Exercise concurrent HTTP delivery independently of Alertmanager's repeat timer.
            concurrent_entry = alert("E03Concurrent" + uuid4().hex[:10])

            def concurrent_send():
                with httpx.Client(
                    base_url=ORIGIN, verify=context, timeout=30, trust_env=False
                ) as client:
                    result = client.post(
                        "/api/intake/alertmanager",
                        headers=machine,
                        json={
                            "version": "4",
                            "status": "firing",
                            "alerts": [concurrent_entry],
                        },
                    )
                    return result.status_code, result.json().get("counts", {})

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(lambda _: concurrent_send(), range(4)))
            if (
                any(status != 200 for status, _ in outcomes)
                or sum(counts.get("accepted", 0) for _, counts in outcomes) != 1
                or sum(counts.get("duplicate", 0) for _, counts in outcomes) != 3
            ):
                raise RuntimeError(
                    "Concurrent firing did not settle as one admission and three duplicates"
                )
            concurrent_receipt = wait_for(
                "concurrent accepted receipt",
                lambda: find(concurrent_entry["labels"]["alertname"], "accepted"),
            )
            concurrent_rows = db(
                "SELECT json_build_object('episodes',count(*),'uid',min(workload_uid),"
                "'incidents',count(DISTINCT incident_id),'runs',count(DISTINCT run_id)) "
                f"FROM alert_episodes WHERE fingerprint='{sql_fingerprint(concurrent_entry['fingerprint'])}'"
            )
            if concurrent_rows != {
                "episodes": 1,
                "uid": uid,
                "incidents": 1,
                "runs": 1,
            }:
                raise RuntimeError(
                    "Concurrent delivery did not bind exactly one episode/incident/Run"
                )
            cancel(concurrent_receipt["incident_id"], concurrent_receipt["run_id"])
            next_entry = dict(concurrent_entry)
            next_entry["startsAt"] = datetime.now(timezone.utc).isoformat()
            response = send([next_entry])
            if response.status_code != 200 or response.json()["counts"].get("accepted") != 1:
                raise RuntimeError("A later firing episode was not admitted separately")
            newer_rows = db(
                "SELECT json_build_object('episodes',count(*),'incidents',count(DISTINCT incident_id),"
                "'runs',count(DISTINCT run_id)) FROM alert_episodes "
                f"WHERE fingerprint='{sql_fingerprint(concurrent_entry['fingerprint'])}'"
            )
            if newer_rows != {"episodes": 2, "incidents": 2, "runs": 2}:
                raise RuntimeError("Distinct episode starts did not preserve separate work")
            newer = next(
                item
                for item in page()
                if item["fingerprint"] == concurrent_entry["fingerprint"]
                and item["outcome"] == "accepted"
                and item["run_id"] != concurrent_receipt["run_id"]
            )
            cancel(newer["incident_id"], newer["run_id"])
            receipt["checks"]["direct_concurrent_and_new_episode"] = {
                "concurrent_outcomes": outcomes,
                "first": concurrent_receipt["run_id"],
                "second": newer["run_id"],
                "persisted": newer_rows,
            }
            name = "E03Sender" + uuid4().hex[:10]
            command(
                "exec",
                "-T",
                "alertmanager",
                "amtool",
                "--alertmanager.url=http://localhost:9093",
                "alert",
                "add",
                f"alertname={name}",
                "namespace=demo",
                "deployment=checkout",
            )
            first = wait_for("real Alertmanager firing", lambda: find(name, "accepted"))
            repeat = wait_for(
                "real Alertmanager repeat", lambda: find(name, "duplicate"), timeout=90
            )
            if (first["incident_id"], first["run_id"]) != (
                repeat["incident_id"],
                repeat["run_id"],
            ):
                raise RuntimeError("Sender repeat changed incident or Run identity")
            incident = api.get(f"/api/incidents/{first['incident_id']}").json()
            if incident["workload"]["uid"] != uid:
                raise RuntimeError("Sender incident is not bound to the live Deployment UID")
            episode = db(
                "SELECT json_build_object('count',count(*),'uid',min(workload_uid),"
                "'runs',(SELECT count(*) FROM investigation_runs WHERE incident_id="
                f"'{sql_uuid(first['incident_id'])}')) FROM alert_episodes WHERE incident_id='{sql_uuid(first['incident_id'])}'"
            )
            if episode != {"count": 1, "uid": uid, "runs": 1}:
                raise RuntimeError(
                    "Sender episode did not persist exactly one UID-bound initial Run"
                )
            receipt["checks"]["real_sender_repeat_uid_binding"] = {
                "incident_id": first["incident_id"],
                "run_id": first["run_id"],
                "episode": episode,
            }
            # Manual API requests fill real Job reservations, not fabricated database rows.
            manual = []
            for index in range(limits["J"] - 1):
                response = api.post(
                    "/api/incidents",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                    json={
                        "workload_id": incident["workload_id"],
                        "title": f"E03 capacity {index}",
                        "description": "Disposable lab",
                    },
                )
                if response.status_code != 201 or response.json()["queue_reason"] is not None:
                    raise RuntimeError(
                        f"Manual reservation {index} failed: HTTP {response.status_code}"
                    )
                manual.append(response.json())
            deferred = []
            entries = []
            for index in range(limits["D"]):
                entry = alert(f"E03Deferred{uuid4().hex[:12]}")
                response = send([entry])
                if response.status_code != 200 or response.json()["counts"].get("deferred") != 1:
                    raise RuntimeError(
                        f"Deferred admission {index} failed: HTTP {response.status_code}"
                    )
                item = wait_for(
                    "deferred receipt",
                    lambda: find(entry["labels"]["alertname"], "deferred"),
                )
                state = run_data(item["run_id"])
                if state["queue_reason"] != "capacity" or state["jobs"]:
                    raise RuntimeError("Deferred Run has unexpected reservation state")
                entries.append(entry)
                deferred.append(item)
            receipt["steps"].append(
                {
                    "step": "real_deferred_reservations",
                    "runs": [x["run_id"] for x in deferred],
                }
            )
            overflow = alert("E03Overflow" + uuid4().hex[:10])
            rejected = alert("E03OutOfScope" + uuid4().hex[:10], deployment="not-configured-e03")
            before_mixed = counts()
            mixed_batch = [entries[0], rejected, overflow]
            response = send(mixed_batch)
            if response.status_code != 503:
                raise RuntimeError(
                    f"Full J/D mixed batch must return retryable 503, got {response.status_code}"
                )
            duplicate = wait_for(
                "saturated duplicate",
                lambda: find(entries[0]["labels"]["alertname"], "duplicate"),
            )
            scoped = wait_for(
                "out-of-scope receipt",
                lambda: find(rejected["labels"]["alertname"], "rejected"),
            )
            retry = wait_for(
                "capacity retryable receipt",
                lambda: find(overflow["labels"]["alertname"], "retryable"),
            )
            if (
                duplicate["run_id"] != deferred[0]["run_id"]
                or scoped["reason"] != "out_of_scope"
                or retry["reason"] != "capacity_exhausted"
            ):
                raise RuntimeError(
                    "Mixed batch outcomes did not preserve dedup, scope and retry semantics"
                )
            after_mixed = counts()
            if any(after_mixed[key] != before_mixed[key] for key in ("incidents", "runs", "jobs")):
                raise RuntimeError(
                    "Saturated mixed batch created partial incident, Run or Job rows"
                )
            absent = db(
                f"SELECT json_build_object('episodes',count(*)) FROM alert_episodes WHERE fingerprint='{sql_fingerprint(overflow['fingerprint'])}'"
            )
            if absent["episodes"] != 0:
                raise RuntimeError("Overflow consumed an episode before capacity became available")
            receipt["checks"]["mixed_batch_saturated"] = {
                "http_status": 503,
                "duplicate_run": duplicate["run_id"],
                "rejected_reason": scoped["reason"],
                "retry_reason": retry["reason"],
                "overflow_episodes": 0,
            }
            cancel(deferred[-1]["incident_id"], deferred[-1]["run_id"])
            response = send(mixed_batch)
            if response.status_code != 200 or response.json()["counts"] != {
                "duplicate": 2,
                "deferred": 1,
            }:
                raise RuntimeError(
                    "Full mixed-batch retry did not deduplicate committed alerts and admit overflow"
                )
            retried = wait_for(
                "accepted overflow retry",
                lambda: find(overflow["labels"]["alertname"], "deferred"),
            )
            response = send(mixed_batch)
            if response.status_code != 200 or response.json()["counts"] != {"duplicate": 3}:
                raise RuntimeError("Accepted full-batch retry did not deduplicate")
            receipt["checks"]["retry_once"] = {
                "incident_id": retried["incident_id"],
                "run_id": retried["run_id"],
            }
            # Recreate the stopped worker without executing a cycle or disturbing dependencies.
            reserved_before = [run_data(item["run_id"]) for item in deferred[:-1] + [retried]]
            manual_before = run_data(manual[0]["run_id"])
            worker_before = command("ps", "--all", "-q", "worker")
            if (
                len(worker_before.splitlines()) != 1
                or manual_before["state"] != "queued"
                or len(manual_before["jobs"]) != 1
                or manual_before["jobs"][0]["state"] != "queued"
            ):
                raise RuntimeError("Expected one stopped worker and one queued manual reservation")
            old_image = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Image}}", worker_before],
                text=True,
                timeout=30,
            ).strip()
            old_env = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", "--format", "{{json .Config.Env}}", worker_before],
                    text=True,
                    timeout=30,
                )
            )
            worker_restore_allowed = False
            command("up", "--no-start", "--no-deps", "--force-recreate", "worker")
            worker_after = command("ps", "--all", "-q", "worker")
            if len(worker_after.splitlines()) != 1 or worker_after == worker_before:
                raise RuntimeError("Compose did not replace the stopped worker container")
            new_image = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Image}}", worker_after],
                text=True,
                timeout=30,
            ).strip()
            receipt["worker_recreation_images"] = {
                "old_image_id": old_image,
                "new_image_id": new_image,
            }
            if not old_image or new_image != old_image:
                raise RuntimeError("Recreated worker image differs from the original")
            new_env = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", "--format", "{{json .Config.Env}}", worker_after],
                    text=True,
                    timeout=30,
                )
            )
            relevant = ("DEFERRED_RUN_LIMIT", "COLLECTOR_DELAY_SECONDS")

            def relevant_env(env):
                return {
                    key: value
                    for item in env
                    for key, _, value in [item.partition("=")]
                    if key in relevant
                }

            receipt["worker_recreation_environment"] = {
                "old": relevant_env(old_env),
                "new": relevant_env(new_env),
            }

            if relevant_env(new_env) != relevant_env(old_env):
                raise RuntimeError("Recreated worker environment differs from the original")
            worker_restore_allowed = True
            reserved_after = [run_data(item["run_id"]) for item in deferred[:-1] + [retried]]
            if reserved_before != reserved_after or run_data(manual[0]["run_id"]) != manual_before:
                raise RuntimeError(
                    "Queued reservations changed while the worker was recreated stopped"
                )
            if any(row["queue_reason"] != "capacity" or row["jobs"] for row in reserved_after):
                raise RuntimeError("Deferred reservations did not survive worker recreation")
            receipt["steps"].append(
                {
                    "step": "stopped_worker_recreated",
                    "reserved_runs": reserved_after,
                    "old_container": worker_before,
                    "new_container": worker_after,
                    "old_image_id": old_image,
                    "new_image_id": new_image,
                }
            )
            cancel(manual[0]["incident_id"], manual[0]["run_id"])
            active_jobs = db(
                "SELECT json_build_object('count',count(*)) FROM jobs "
                "WHERE state IN ('queued','running')"
            )["count"]
            if active_jobs != limits["J"] - 1:
                raise RuntimeError("Expected exactly one free Job slot before worker promotion")
            receipt["checks"]["automatic_promotion_context"] = {
                "initial_available_slots": 1,
                "deferred_runs": len(reserved_after),
                "manual_promotion_calls": 0,
                "competing_admissions": False,
            }
            command("start", "worker")
            worker_stopped = False
            new_config = config("worker")
            receipt["steps"][-1]["new_config"] = new_config
            if new_config != limits:
                raise RuntimeError("Recreated worker configuration differs from captured settings")
            promoted = deferred[:-1] + [retried]
            for item in promoted:
                wait_for(
                    "automatic promotion of the same Run",
                    lambda item=item: run_data(item["run_id"])["jobs"],
                    timeout=600,
                )
            states = [run_data(item["run_id"]) for item in promoted]
            if any(len(row["jobs"]) != 1 for row in states):
                raise RuntimeError("A promoted Run has zero or multiple collection Jobs")
            expected = sorted(states, key=lambda row: (row["created_at"], row["id"]))
            actual = sorted(
                states,
                key=lambda row: (row["jobs"][0]["created_at"], row["jobs"][0]["id"]),
            )
            if [x["id"] for x in expected] != [x["id"] for x in actual]:
                raise RuntimeError(
                    "Automatic promotions were not oldest-first across worker cycles"
                )
            receipt["checks"]["automatic_same_run_promotion_and_restart"] = states
            receipt["checks"]["oldest_first_without_competing_admissions"] = [
                row["id"] for row in actual
            ]
            listing = json.dumps(page())
            if token in listing or "e03-probe-private-annotation" in listing:
                raise RuntimeError("Receipt API exposed sender credential or annotation")
            receipt["checks"]["receipt_redaction"] = True
            if args.replace_owned_checkout:

                def idle():
                    active = db(
                        "SELECT json_build_object('jobs',(SELECT count(*) FROM jobs "
                        "WHERE state IN ('queued','running')),'deferred',(SELECT count(*) "
                        "FROM investigation_runs WHERE state='queued' AND queue_reason='capacity'))"
                    )
                    return active == {"jobs": 0, "deferred": 0}

                wait_for("probe work to drain before replacement", idle, timeout=900)
                worker_stopped = True
                command("stop", "worker")
                replace_owned_checkout(api, send, first, deployment, receipt)
            else:
                receipt["limits"].append(
                    "UID replacement was not requested and remains unexercised."
                )
        receipt["status"] = "passed_exercised_checks"
    except Exception as exc:
        receipt["status"] = "failed"
        # Do not serialize exceptions: HTTP/library messages can contain credentials.
        receipt["failure_type"] = type(exc).__name__
        if isinstance(exc, (RuntimeError, AssertionError)):
            receipt["failure"] = str(exc)
        else:
            receipt["failure"] = "Prerequisite or operation failed; inspect local lab privately."
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        if receipt["status"] == "running":
            receipt["status"] = "interrupted"
        cleanup_failed = []
        for container in owned_containers:
            try:
                result = subprocess.run(
                    ["docker", "rm", "-f", container],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode:
                    exists = subprocess.run(
                        ["docker", "inspect", "--format", "{{.Id}}", container],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if exists.returncode == 0:
                        cleanup_failed.append(container)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed.append(container)
        for path in owned_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed.append(path.name)
        if cleanup_failed:
            receipt["sender_cleanup"] = {"failed": cleanup_failed}
            if receipt["status"] != "interrupted":
                receipt["status"] = "failed"
        elif owned_containers or owned_files:
            receipt["sender_cleanup"] = (
                "removed only probe-created sender containers and configuration"
            )
        if worker_stopped and worker_initially_running and not worker_restore_allowed:
            receipt["worker_restore"] = (
                "not started; recreated worker identity or configuration unverified"
            )
        elif worker_stopped and worker_initially_running:
            try:
                command("start", "worker")
            except Exception:
                receipt["worker_restore"] = "failed; start the project worker manually"
                if receipt["status"] != "interrupted":
                    receipt["status"] = "failed"
            else:
                receipt["worker_restore"] = "started"
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            print(f"Sanitized partial/final receipt: {args.output}")
        except Exception:
            if not active_exception:
                raise
        if receipt["status"] == "failed" and sys.exc_info()[0] is None:
            raise RuntimeError(
                "Probe cleanup or worker restoration failed; inspect the sanitized receipt"
            )


if __name__ == "__main__":
    main()
