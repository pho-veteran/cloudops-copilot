"""Qualify the release backup/restore procedure in a disposable release-image lab.

Run from a verified release install directory on Linux with CLOUDOPS_LAB_PROJECT,
CLOUDOPS_RELEASE_BUNDLE, CLOUDOPS_HOST_ENV and CLOUDOPS_RELEASE_RESTORE=1 set, after
scripts/bootstrap-integration.py. Phases share state under tmp/release-restore/.
Passwords are read from stdin (Operator, then Viewer) and never written to receipts.
Only the project's own cloudops_restore_test database is restored.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from uuid import uuid4

RESTORE_DB = "cloudops_restore_test"
ORIGIN = "https://localhost:8443"
STANDBY = "checkout-standby"
PIN_MISSING = "Pinned executor authority is missing or changed"


def digest_query(table):
    return f"select md5(coalesce(string_agg(t::text, chr(10) order by t::text), '')) from {table} t"


# Tables whose rows are compared one by one between cutoff, restore and live state.
KEY_TABLES = (
    "incidents",
    "incident_activity",
    "investigation_runs",
    "evidence",
    "action_plans",
    "recovery_executions",
    "jobs",
)
# Tables that restore cleanup or bootstrap is expected to change. Their identifiers are
# compared; their content is not, because cleanup owns those changes.
CLEANUP_TABLES = {
    "jobs": "restored recovery jobs retired",
    "job_attempts": "running restored attempts closed",
    "recovery_executions": "active restored executions retired",
    "action_plans": "proposed and retired approved plans invalidated",
    "recovery_verifications": "restored verification authority retired",
    "workloads": "bootstrap scope synchronization counters",
    "workload_scope_state": "bootstrap scope synchronization counters",
}
# Tables that restore cleanup empties or that live processes rewrite; not compared.
VOLATILE_TABLES = {
    "sessions": "restored sessions revoked; new sign-ins add rows",
    "plan_reviews": "restored review receipts retired; new reads add rows",
    "worker_heartbeats": "live worker heartbeat",
}
MAINTENANCE_WORDS = ("maintenance", "enable_target", "recovery_mode", "release_uncertain")


def die(message):
    raise SystemExit(f"release restore: {message}")


def guard():
    if os.name != "posix":
        die("run from a Linux host or WSL; trusted CLIs require POSIX peer credentials.")
    if os.environ.get("CLOUDOPS_RELEASE_RESTORE") != "1":
        die("set CLOUDOPS_RELEASE_RESTORE=1 so every service uses the owned restore database.")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import local_lab

    if "compose.release-restore.yaml" not in local_lab.COMPOSE:
        die("the restore overlay is not selected.")
    return local_lab


# Bound by main(); importing this module for tests has no side effects.
LAB = ROOT = OUT = PRIVATE = None


def run(args, *, stdin=None, check=True, timeout=600):
    result = subprocess.run(
        args,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        cwd=ROOT,
        timeout=timeout,
        text=not isinstance(stdin, bytes),
    )
    if check and result.returncode != 0:
        err = result.stderr if isinstance(result.stderr, str) else result.stderr.decode()
        die(f"{' '.join(map(str, args[-6:]))} failed: {err.strip()[-800:]}")
    return result


def dc(*args, **kwargs):
    return run([*LAB.COMPOSE, *args], **kwargs)


def out(*args, **kwargs):
    value = dc(*args, **kwargs).stdout
    return value if isinstance(value, str) else value.decode()


def jsonout(*args, **kwargs):
    text = out(*args, **kwargs)
    return json.loads(text[text.index("{") :])


def kubectl(*args):
    return out("exec", "-T", "k3s", "/bin/kubectl", *args)


def sql(query):
    return out("exec", "-T", "db", "psql", "-U", "cloudops", "-d", RESTORE_DB, "-At", "-c", query)


def write(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    return path


def read(name, default=None):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else default


def state():
    return read("state.json", {})


def save_state(value):
    write("state.json", value)


def bound_state():
    """Destructive phases run only in the project that prepare recorded."""
    data = state()
    if data.get("project") != LAB.PROJECT:
        die("this project was not prepared by the driver; refusing destructive steps.")
    return data


def passwords():
    lines = [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
    if not all(lines):
        die("provide the Operator and Viewer passwords on stdin.")
    return lines


# ---------- identities and fingerprints ----------


def database_identity():
    return {
        "database": sql("select current_database()").strip(),
        "server_version": sql("show server_version").strip(),
        "alembic_head": sql("select version_num from alembic_version").strip(),
        "observed_at": sql("select now()").strip(),
        "txid": sql("select pg_current_xact_id()::text").strip(),
    }


def connections_owned(services):
    """Each running service's own DATABASE_URL must name the restore database (value not kept)."""
    result = {}
    for service in services:
        value = out("exec", "-T", service, "printenv", "DATABASE_URL").strip()
        result[service] = value.rsplit("/", 1)[-1] == RESTORE_DB
    if not all(result.values()):
        die(f"a service connects outside the owned restore database: {result}")
    return result


def fingerprint():
    tables = sql(
        "select table_name || '|' || bool_or(column_name = 'id') from information_schema.columns "
        "where table_schema = 'public' group by table_name order by table_name"
    ).split()
    parts = []
    for item in tables:
        table, has_id = item.split("|")
        keyed = has_id == "true"
        ids = "md5(coalesce(string_agg(id::text, ',' order by id::text), ''))" if keyed else "''"
        rows = (
            "coalesce(json_object_agg(id::text, md5(t::text)), '{}')::text"
            if table in KEY_TABLES and keyed
            else "'{}'"
        )
        parts.append(
            f"select '{table}' || chr(9) || count(*) || chr(9) || "
            f"md5(coalesce(string_agg(t::text, chr(10) order by t::text), '')) || chr(9) || "
            f'{ids} || chr(9) || {rows} from public."{table}" t'
        )
    result = {}
    for row in sql(" union all ".join(parts)).strip().splitlines():
        table, count, digest, ids, rows = row.split("\t")
        result[table] = {"rows": int(count), "md5": digest, "ids_md5": ids}
        if table in KEY_TABLES:
            result[table]["row_md5"] = json.loads(rows)
    return result


def evidence_digests():
    rows = sql("select id::text || ' ' || coalesce(sha256, '') from evidence order by id")
    return dict(line.split(" ", 1) for line in rows.split("\n") if line.strip())


def executions():
    rows = sql(
        "select json_build_object('id', id, 'state', state, 'holds_lock', holds_lock, "
        "'maintenance_released', maintenance_released, 'outcome', result->>'outcome') "
        "from recovery_executions order by created_at"
    )
    return [json.loads(line) for line in rows.splitlines() if line.strip()]


def writer_targets():
    return json.loads((LAB.LAB_DIR / "writer/workloads.json").read_text())


def deployments():
    result = {}
    for target in writer_targets():
        item = json.loads(kubectl("get", "deployment", target["name"], "-n", "demo", "-o", "json"))
        annotations = item["metadata"].get("annotations") or {}
        result[target["name"]] = {
            "uid": item["metadata"]["uid"],
            "resource_version": item["metadata"]["resourceVersion"],
            "generation": item["metadata"]["generation"],
            "replicas": item["spec"]["replicas"],
            "fence": {k: v for k, v in annotations.items() if "fence" in k},
            "ready": item.get("status", {}).get("readyReplicas", 0),
        }
    return result


def written(before, after):
    """Spec writes and fences change generation, replicas or annotations; status updates do not."""
    keys = ("uid", "generation", "replicas", "fence")
    return {
        name: {k: before[name][k] for k in keys} != {k: after[name][k] for k in keys}
        for name in before
    }


def journal():
    code = (
        "import hashlib,json,sqlite3;"
        "db=sqlite3.connect('file:/var/lib/cloudops-executor/authority.sqlite?mode=ro',uri=True);"
        "q=lambda s:db.execute(s).fetchall();"
        "pin=open('/run/cloudops-executor-identity/identity.json','rb').read();"
        "print(json.dumps({'deployment_id':q(\"select value from control where key='deployment_id'\")[0][0],"
        "'pin_sha256':hashlib.sha256(pin).hexdigest(),"
        "'attempts':dict(q('select state,count(*) from attempts group by state')),"
        "'attempt_ids':[r[0] for r in q('select id from attempts order by rowid')],"
        "'audit_rows':q('select count(*) from audit')[0][0],"
        "'targets':{r[0]:{'epoch':r[1],'enabled':bool(r[2])} for r in q('select uid,epoch,enabled from targets')},"
        "'recovery_mode':q(\"select value from control where key='recovery_mode'\")[0][0]}))"
    )
    return json.loads(out("exec", "-T", "executor", "python", "-c", code))


def maintenance(operation, *, reason="", evidence=(), request_id=None, targets=None):
    args = ["exec", "-T", "executor", "python", "-m", "cloudops.features.recovery.maintenance"]
    args.append(operation)
    if operation not in ("status", "target_status"):
        for uid in targets if targets is not None else [t["uid"] for t in writer_targets()]:
            args += ["--target", uid]
        args += ["--reason", reason]
        for item in evidence:
            args += ["--evidence", item]
    if request_id:
        args += ["--request-id", request_id]
    result = dc(*args, check=False, timeout=180)
    text = result.stdout
    try:
        return json.loads(text[text.index("{") :])
    except ValueError:
        return {"status": "unavailable", "stderr": result.stderr.strip()[-400:]}


def audit_entries(request_ids):
    wanted = set(request_ids)
    rows = []
    for row in maintenance("status").get("audit", []):
        if row["id"] in wanted:
            request = json.loads(row["request"])
            rows.append(
                {
                    "request_id": row["id"],
                    "operation": request.get("operation"),
                    "targets": request.get("targets"),
                    "reason": request.get("reason"),
                    "evidence": request.get("evidence"),
                    "actor": json.loads(row["actor"]),
                    "status": json.loads(row["result"]).get("status"),
                    "observed_at": row["observed_at"],
                }
            )
    return rows


def dispatch_admin(*args):
    if args[0] == "status":
        return jsonout("run", "--rm", "-T", "dispatch-admin", *args)
    return dc("run", "--rm", "-T", "dispatch-admin", *args).stdout


def permit():
    status = dispatch_admin("status")
    p = status["permit"]
    if p is None:
        return None
    return {
        "authority_id": p["authority_id"],
        "generation": p["generation"],
        "mode": p["mode"],
        "allowed_job_kinds": p["allowed_job_kinds"],
        "denial": status.get("denial"),
    }


def bundle_identity():
    manifest = json.loads(
        (Path(os.environ["CLOUDOPS_RELEASE_BUNDLE"]) / "manifest.json").read_text()
    )
    return {
        "revision": manifest["revision"],
        "images": {k: v["id"] for k, v in manifest["images"].items()},
        "sha256": manifest["sha256"],
    }


def scope_digests():
    return {
        kind: hashlib.sha256((LAB.LAB_DIR / kind / "workloads.json").read_bytes()).hexdigest()
        for kind in ("reader", "writer")
    }


def worker_alive():
    container = f"{LAB.PROJECT}-worker-1"
    status = run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container], check=False
    ).stdout.strip()
    beat = sql("select coalesce(max(observed_at)::text, '') from worker_heartbeats").strip()
    return {"container": status, "heartbeat": beat}


def maintenance_surface():
    """No API route offers maintenance; the API container has no maintenance channel."""
    code = (
        "import json; from cloudops.entrypoints.api import create_app; "
        f"words={MAINTENANCE_WORDS!r}; "
        "print(json.dumps(sorted(p for p in create_app().openapi()['paths'] "
        "if any(w in p for w in words))))"
    )
    routes = json.loads(out("exec", "-T", "api", "python", "-c", code).strip().splitlines()[-1])
    channel = dc(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "cloudops.features.recovery.maintenance",
        "target_status",
        check=False,
    )
    return {
        "api_routes_matching": routes,
        "api_channel_returncode": channel.returncode,
        "api_channel_message": (channel.stderr or channel.stdout).strip()[-160:],
    }


# ---------- comparison ----------


def compare(manifest, restored, live):
    """Classify restored tables against the cutoff and list what the restore lost."""
    tables = {}
    for table, expected in manifest["fingerprint"].items():
        got = restored.get(table)
        if got is None:
            tables[table] = {"result": "missing"}
            continue
        if got["md5"] == expected["md5"]:
            result = "identical"
        elif table in VOLATILE_TABLES:
            result = "not compared: " + VOLATILE_TABLES[table]
        elif got["ids_md5"] == expected["ids_md5"] and table in CLEANUP_TABLES:
            result = "identifiers retained; content changed by " + CLEANUP_TABLES[table]
        else:
            result = "unexpected change"
        tables[table] = {
            "result": result,
            "rows_cutoff": expected["rows"],
            "rows_restored": got["rows"],
        }
    rows = {}
    for table in KEY_TABLES:
        cutoff = manifest["fingerprint"].get(table, {}).get("row_md5", {})
        now = restored.get(table, {}).get("row_md5", {})
        later = live["fingerprint"].get(table, {}).get("row_md5", {})
        rows[table] = {
            "lost_after_cutoff": sorted(set(later) - set(now)),
            "changed_after_cutoff": sorted(
                i for i in cutoff if i in later and later[i] != cutoff[i]
            ),
            "changed_by_restore_cleanup": sorted(
                i for i in cutoff if i in now and now[i] != cutoff[i]
            ),
            "missing_from_cutoff": sorted(set(cutoff) - set(now)),
            "deleted_after_cutoff": sorted(set(cutoff) - set(later)),
        }
    return tables, rows


# ---------- HTTPS client ----------


def client():
    import httpx

    return httpx.Client(
        base_url=ORIGIN,
        verify=ssl.create_default_context(cafile=str(OUT / "ca.crt")),
        trust_env=False,
        timeout=30,
    )


def login(session, username, password):
    response = session.post(
        "/api/login", json={"username": username, "password": password}, headers={"Origin": ORIGIN}
    )
    if response.status_code != 200:
        die(f"{username} sign-in returned {response.status_code}")
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def code_of(response):
    try:
        return response.json().get("error", {}).get("code")
    except ValueError:
        return None


def workload_ids(session):
    items = session.get("/api/workloads").json()["items"]
    uids = {t["uid"]: t["name"] for t in writer_targets()}
    found = {uids[i["uid"]]: i["id"] for i in items if i["uid"] in uids}
    if len(found) != len(uids):
        die("every writer target must be a configured application workload.")
    return found


def incident(session, headers, workload_id, title):
    response = session.post(
        "/api/incidents",
        json={
            "workload_id": workload_id,
            "title": title,
            "description": "Release restore qualification record.",
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    if response.status_code != 201:
        die(f"incident admission returned {response.status_code}: {response.text[:300]}")
    return response.json()


def wait_run(session, incident_id, run_id, seconds=240):
    deadline = time.monotonic() + seconds
    run_view = {}
    while time.monotonic() < deadline:
        run_view = session.get(f"/api/incidents/{incident_id}/runs/{run_id}").json()
        if run_view.get("state") not in ("queued", "collecting"):
            break
        time.sleep(3)
    return run_view


def propose(session, headers, incident_id, replicas, *, wait_cooldown=False):
    deadline = time.monotonic() + 90
    while True:
        response = session.post(
            f"/api/incidents/{incident_id}/plans",
            json={"replicas": replicas, "rationale": "Release restore qualification scale"},
            headers=headers,
        )
        if not wait_cooldown or code_of(response) != "target_cooldown":
            return response
        if time.monotonic() > deadline:
            return response
        time.sleep(5)


def approve(session, headers, plan):
    review = session.get(f"/api/plans/{plan['id']}").json()
    body = {
        "version": review["plan"]["version"],
        "digest": review["plan"]["digest"],
        "review_token": review["review_token"],
        "decision": "approve",
    }
    return session.post(f"/api/plans/{plan['id']}/decision", json=body, headers=headers)


def wait_execution(session, incident_id, seconds=180):
    deadline = time.monotonic() + seconds
    rows = []
    while time.monotonic() < deadline:
        rows = session.get(f"/api/incidents/{incident_id}/executions").json()
        if rows and rows[0]["state"] not in ("queued", "executing"):
            return rows
        time.sleep(1)
    return rows


def stale_dispatches(epoch, execution_id):
    """Send stale dispatches with the worker's own key; the executor must refuse without writing."""
    primary = writer_targets()[0]["uid"]
    code = f"""
import json, os, uuid, datetime, urllib.request, urllib.error
key = open('/run/cloudops-dispatch/key').read().strip()
def send(body):
    request = urllib.request.Request(os.environ['EXECUTOR_URL'] + '/dispatch',
        data=json.dumps(body).encode(), method='POST',
        headers={{'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {{'status': response.status, 'body': json.loads(response.read())}}
    except urllib.error.HTTPError as error:
        return {{'status': error.code, 'code': json.loads(error.read()).get('error', {{}}).get('code')}}
expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=4)).isoformat()
base = dict(uid={primary!r}, epoch={epoch!r}, resource_version='1', replicas=1, before={{}},
    plan_digest='0' * 64, expires_at=expires)
result = {{'retired_epoch_new_identity': send(dict(base, execution_id=str(uuid.uuid4())))}}
if {execution_id!r}:
    result['restored_identity_changed_content'] = send(dict(base, execution_id={execution_id!r}))
print(json.dumps(result))
"""
    return json.loads(out("exec", "-T", "worker", "python", "-c", code).strip().splitlines()[-1])


# ---------- phases ----------


def phase_prepare(args):
    if LAB.PROJECT in ("cloudops-e01-https", LAB.OWNED_PROJECT):
        die("use a new disposable project.")
    if (
        run(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={LAB.PROJECT}",
            ]
        ).stdout.strip()
        and state().get("project") != LAB.PROJECT
    ):
        # bootstrap-integration creates only K3s before prepare; refuse adopting other work.
        services = out("ps", "-a", "--format", "{{.Service}}").split()
        if set(services) - {"k3s"}:
            die("the project already has application containers; use a new project.")
    config = json.loads(out("--profile", "*", "config", "--format", "json"))
    if config["services"]["db"]["environment"].get("POSTGRES_DB") != RESTORE_DB:
        die("the database service must create the owned restore database.")
    for name, service in config["services"].items():
        url = (service.get("environment") or {}).get("DATABASE_URL")
        if url and not url.endswith("/" + RESTORE_DB):
            die(f"{name} would connect outside the owned restore database.")
    run(
        [*LAB.COMPOSE, "exec", "-T", "k3s", "/bin/kubectl", "apply", "-f", "-"],
        stdin=(ROOT / "config/lab/restore-standby.yaml").read_text(),
    )
    kubectl("rollout", "status", f"deployment/{STANDBY}", "-n", "demo", "--timeout=180s")
    standby = json.loads(kubectl("get", "deployment", STANDBY, "-n", "demo", "-o", "json"))
    entry = {
        "cluster": "local-k3s",
        "namespace": "demo",
        "name": STANDBY,
        "uid": standby["metadata"]["uid"],
        "source": "live_kubernetes",
    }
    for kind in ("writer", "reader"):
        path = LAB.LAB_DIR / kind / "workloads.json"
        items = [i for i in json.loads(path.read_text()) if i["name"] != STANDBY]
        LAB.private_write(path, json.dumps([*items, entry], indent=2) + "\n")
    dc(
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "240",
        "api",
        "worker",
        "executor",
        "https",
        timeout=900,
    )
    owned = connections_owned(("api", "worker", "executor"))
    operator_password, viewer_password = passwords()
    for user, password in (("operator", operator_password), ("viewer", viewer_password)):
        dc(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "cloudops.entrypoints.cli",
            "account",
            user,
            "--role",
            user,
            "--password-stdin",
            stdin=password + "\n",
        )
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    dc("cp", "https:/data/caddy/pki/authorities/local/root.crt", str(OUT / "ca.crt"))
    save_state({"project": LAB.PROJECT, "targets": writer_targets()})
    receipt = {
        "project": LAB.PROJECT,
        "bundle": bundle_identity(),
        "database": database_identity(),
        "connections_use_restore_database": owned,
        "targets": writer_targets(),
        "scope_sha256": scope_digests(),
        "permit": permit(),
        "maintenance": maintenance("status"),
        "journal": journal(),
        "deployments": deployments(),
        "ca_sha256": hashlib.sha256((OUT / "ca.crt").read_bytes()).hexdigest(),
    }
    print(write("prepare.json", receipt))


def phase_reconcile(args):
    bound_state()
    label = args.label
    observed = {
        "deployments": deployments(),
        "journal": journal(),
        "maintenance": maintenance("status"),
        "executions": executions(),
    }
    name = f"reconcile-{label}.json"
    write(name, {"observed": observed})
    evidence = [f"local://release-restore/{name}"]
    results, request_ids = {}, []
    if args.prove_failure:
        target = next(t for t in writer_targets() if t["name"] == args.prove_failure)
        # Deliberate out-of-protocol lab action: an unready target has no current-state proof.
        kubectl("scale", "deployment", target["name"], "-n", "demo", "--replicas=0")
        denied = maintenance(
            "enable_target",
            targets=[target["uid"]],
            evidence=evidence,
            reason=f"Enable without current-state proof ({label}).",
        )
        request_ids.append(denied.get("request_id"))
        status = next(t for t in maintenance("status")["targets"] if t["uid"] == target["uid"])
        results["proof_unavailable"] = {
            "target": target["name"],
            "status": denied.get("status"),
            "code": denied.get("code"),
            "enabled_after": status["enabled"],
            "recovery_mode_after": status["recovery_mode"],
        }
        kubectl("scale", "deployment", target["name"], "-n", "demo", "--replicas=1")
        kubectl("rollout", "status", f"deployment/{target['name']}", "-n", "demo", "--timeout=180s")
    for target in writer_targets():
        value = maintenance(
            "enable_target",
            targets=[target["uid"]],
            evidence=evidence,
            reason=f"Reconciled current state ({label}); fence and health proof required.",
        )
        results[f"enable_{target['name']}"] = value
        request_ids.append(value.get("request_id"))
    value = maintenance(
        "exit_recovery_mode", evidence=evidence, reason=f"All targets reconciled ({label})."
    )
    results["exit_recovery_mode"] = value
    request_ids.append(value.get("request_id"))
    if args.dispatch_exit:
        dispatch_admin(
            "exit-recovery",
            "--reason",
            f"Restored scope reconciled ({label})",
            "--evidence",
            evidence[0],
        )
    after = {
        "deployments": deployments(),
        "journal": journal(),
        "permit": permit(),
        "executions": executions(),
        "audit": audit_entries(request_ids),
    }
    write(name, {"observed": observed, "results": results, "after": after})
    for key, value in results.items():
        if key != "proof_unavailable" and value.get("status") != "applied":
            die(f"{key} was not applied: {value.get('code')} {value.get('reason')}")
    if args.prove_failure and results["proof_unavailable"]["enabled_after"]:
        die("a target without current-state proof was enabled.")
    print(OUT / name)


def cut_manifest(name, extra):
    dump = PRIVATE / f"{name}.dump"
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = {"database": database_identity(), "fingerprint": fingerprint()}
    data = dc(
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "cloudops",
        "-d",
        RESTORE_DB,
        "-Fc",
        stdin=b"",
        timeout=600,
    ).stdout
    after = {"database": database_identity(), "fingerprint": fingerprint()}
    dump.write_bytes(data)
    os.chmod(dump, 0o600)
    moved = sorted(
        t
        for t in before["fingerprint"]
        if t not in VOLATILE_TABLES
        and before["fingerprint"][t]["md5"] != after["fingerprint"].get(t, {}).get("md5")
    )
    if moved:
        die(f"records changed while the backup ran: {moved}; take the cutoff again.")
    manifest = {
        "schema": "ReleaseRestoreManifest/v1",
        "cutoff": name,
        "project": LAB.PROJECT,
        "bundle": bundle_identity(),
        "database": before["database"],
        "database_after_dump": after["database"],
        "stable_during_dump": "every non-volatile table digest equal before and after pg_dump",
        "dump": {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "format": "pg_dump -Fc",
        },
        "fingerprint": before["fingerprint"],
        "evidence_sha256": evidence_digests(),
        "permit": permit(),
        "journal": journal(),
        "maintenance_targets": maintenance("status")["targets"],
        "targets": writer_targets(),
        "scope_sha256": scope_digests(),
        "deployments": deployments(),
        **extra,
    }
    write(f"manifest-{name}.json", manifest)
    return manifest


def phase_cut(args):
    data = bound_state()
    operator_password, _ = passwords()
    with client() as session:
        headers = login(session, "operator", operator_password)
        ids = workload_ids(session)
        primary = writer_targets()[0]["name"]
        if args.name == "d1":
            main_incident = incident(session, headers, ids[primary], "Release restore primary")
            standby = incident(session, headers, ids[STANDBY], "Release restore standby")
            wait_run(session, main_incident["incident_id"], main_incident["run_id"])
            wait_run(session, standby["incident_id"], standby["run_id"])
            replicas = 2 if deployments()[primary]["replicas"] == 1 else 1
            response = propose(session, headers, main_incident["incident_id"], replicas)
            if response.status_code != 201:
                die(f"proposal returned {response.status_code}: {response.text[:300]}")
            data.update(
                primary_incident=main_incident,
                standby_incident=standby,
                plan=response.json(),
                replicas=replicas,
            )
            extra = {"stage": "plan proposed, not approved", "plan_id": data["plan"]["id"]}
        elif args.name == "d2":
            dc("stop", "worker")
            response = approve(session, headers, data["plan"])
            if response.status_code != 200:
                die(f"approval returned {response.status_code}: {response.text[:300]}")
            rows = session.get(
                f"/api/incidents/{data['primary_incident']['incident_id']}/executions"
            ).json()
            data["execution_id"] = rows[0]["id"]
            extra = {
                "stage": "plan approved, execution queued before dispatch",
                "execution": {k: rows[0][k] for k in ("id", "state", "holds_lock")},
            }
        elif args.name == "d3":
            dc("up", "-d", "--no-build", "--pull", "never", "--wait", "worker")
            rows = wait_execution(session, data["primary_incident"]["incident_id"])
            if not rows or rows[0]["result"].get("outcome") != "acknowledged":
                die(f"execution did not reach an acknowledged outcome: {rows[:1]}")
            extra = {
                "stage": "execution dispatched and acknowledged",
                "execution": {k: rows[0][k] for k in ("id", "state", "holds_lock")},
                "outcome": rows[0]["result"].get("outcome"),
            }
        else:
            die("unknown cut")
        cookie = session.cookies.get("cloudops_session")
    cookies = read("private/cookies.json", {})
    cookies[args.name] = cookie
    write("private/cookies.json", cookies)
    os.chmod(OUT / "private/cookies.json", 0o600)
    save_state(data)
    manifest = cut_manifest(args.name, extra)
    print(json.dumps({"cutoff": args.name, "dump_sha256": manifest["dump"]["sha256"]}))


def phase_activity(args):
    data = bound_state()
    operator_password, _ = passwords()
    with client() as session:
        headers = login(session, "operator", operator_password)
        ids = workload_ids(session)
        later = incident(
            session, headers, ids[writer_targets()[0]["name"]], "Release restore post-cutoff"
        )
        session.post(
            f"/api/incidents/{later['incident_id']}/notes",
            json={"text": "Post-cutoff note"},
            headers=headers,
        )
        session.post(
            f"/api/incidents/{data['primary_incident']['incident_id']}/notes",
            json={"text": "Post-cutoff note on the primary incident"},
            headers=headers,
        )
        wait_run(session, later["incident_id"], later["run_id"])
    data["post_cutoff_incident"] = later
    save_state(data)
    write(
        "live.json",
        {
            "project": LAB.PROJECT,
            "database": database_identity(),
            "fingerprint": fingerprint(),
            "evidence_sha256": evidence_digests(),
            "journal": journal(),
            "executions": executions(),
            "deployments": deployments(),
        },
    )
    print(OUT / "live.json")


def phase_restore(args):
    name = args.name
    data = bound_state()
    manifest = read(f"manifest-{name}.json") or die(f"no manifest for {name}")
    live = read("live.json") or die("record post-cutoff activity first")
    if manifest["project"] != LAB.PROJECT or live.get("project") != LAB.PROJECT:
        die("the manifest or live snapshot belongs to another project.")
    operator_password, viewer_password = passwords()
    receipt = {"cutoff": name, "dump_sha256": manifest["dump"]["sha256"]}
    dump = PRIVATE / f"{name}.dump"
    if hashlib.sha256(dump.read_bytes()).hexdigest() != manifest["dump"]["sha256"]:
        die("dump does not match its manifest.")
    evidence = [f"local://release-restore/manifest-{name}.json"]
    receipt["permit_before"] = permit()
    dispatch_admin("enter-recovery", "--reason", f"Restore {name}", "--evidence", evidence[0])
    enter = maintenance("enter_recovery", reason=f"Restore {name}", evidence=evidence)
    receipt["maintenance_enter"] = {k: enter.get(k) for k in ("status", "request_id", "code")}
    if enter.get("status") != "applied":
        die(f"every writer target must be quarantined before the restore: {enter}")
    dc("stop", "worker", "api", "https", "observer")
    receipt["executor_uses_restore_database"] = connections_owned(("executor",))
    dc(
        "exec",
        "-T",
        "db",
        "pg_restore",
        "-U",
        "cloudops",
        "-d",
        RESTORE_DB,
        "--clean",
        "--if-exists",
        "--no-owner",
        stdin=dump.read_bytes(),
        timeout=600,
    )
    dc("run", "--rm", "--no-deps", "migrate", timeout=600)
    receipt["cleanup"] = jsonout("run", "--rm", "--no-deps", "restore-admin", timeout=300)
    receipt["diagnosis_cleanup"] = out(
        "run",
        "--rm",
        "--no-deps",
        "restore-admin",
        "cloudops.entrypoints.diagnosis_recovery",
        timeout=300,
    ).strip()[-400:]
    restored = fingerprint()
    tables, rows = compare(manifest, restored, live)
    receipt["tables"] = tables
    receipt["rows"] = rows
    receipt["evidence_bytes_equal"] = evidence_digests() == manifest["evidence_sha256"]
    receipt["database"] = database_identity()
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "api", "https", timeout=600)
    receipt["permit_after"] = permit()
    status = maintenance("status")
    receipt["targets_after"] = [
        {k: t[k] for k in ("uid", "enabled", "recovery_mode", "historical_unknown_count")}
        for t in status["targets"]
    ]
    receipt["journal_unknown_attributions"] = status["unknown_attributions"]
    receipt["journal_attempts"] = journal()["attempts"]
    with client() as session:
        cookie = read("private/cookies.json", {}).get(name)
        receipt["restored_session"] = session.get(
            "/api/session", headers={"Cookie": f"cloudops_session={cookie}"}
        ).status_code
    with client() as operator, client() as viewer:
        oh = login(operator, "operator", operator_password)
        login(viewer, "viewer", viewer_password)
        ids = workload_ids(viewer)
        primary = data["primary_incident"]["incident_id"]
        receipt["reads"] = {
            "workloads": {n: viewer.get(f"/api/workloads/{i}").status_code for n, i in ids.items()},
            "primary_incident": viewer.get(f"/api/incidents/{primary}").status_code,
            "primary_run": viewer.get(
                f"/api/incidents/{primary}/runs/{data['primary_incident']['run_id']}"
            ).status_code,
            "standby_incident": viewer.get(
                f"/api/incidents/{data['standby_incident']['incident_id']}"
            ).status_code,
        }
        plans = operator.get(f"/api/incidents/{primary}/plans").json()
        receipt["restored_plans"] = [{k: p[k] for k in ("id", "state")} for p in plans]
        old = next((p for p in plans if p["id"] == data["plan"]["id"]), None)
        if old is not None:
            response = approve(operator, oh, old)
            receipt["old_plan_approval"] = {
                "status": response.status_code,
                "code": code_of(response),
            }
        receipt["executions"] = executions()
        proposals = {}
        for key, inc in (
            ("primary", primary),
            ("standby", data["standby_incident"]["incident_id"]),
        ):
            response = propose(operator, oh, inc, 1)
            proposals[key] = {"status": response.status_code, "code": code_of(response)}
        receipt["new_proposals_while_quarantined"] = proposals
    receipt["maintenance_surface"] = maintenance_surface()
    # Restart and replay attempts with application processes stopped must not write.
    before = {"deployments": deployments(), "journal": journal()}
    dc("stop", "api", "https")
    dc("restart", "executor")
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "executor", timeout=300)
    replay = maintenance(
        "enter_recovery",
        reason=f"Restore {name}",
        evidence=evidence,
        request_id=enter.get("request_id"),
    )
    receipt["maintenance_replay"] = {
        "replayed": replay.get("replayed"),
        "status": replay.get("status"),
    }
    if replay.get("replayed") is not True:
        die("the repeated maintenance request was not answered from its durable receipt.")
    receipt["cleanup_replay"] = jsonout("run", "--rm", "--no-deps", "restore-admin", timeout=300)
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "api", "https", timeout=600)
    # The worker that ran before the restore starts against the restored database.
    jobs_before = sql(digest_query("jobs"))
    attempts_before = sql(digest_query("job_attempts"))
    alive_before = worker_alive()
    dc("up", "-d", "--no-build", "--pull", "never", "worker")
    time.sleep(25)
    alive_after = worker_alive()
    receipt["old_worker"] = {
        "jobs_unchanged": sql(digest_query("jobs")) == jobs_before,
        "attempts_unchanged": sql(digest_query("job_attempts")) == attempts_before,
        "container": alive_after["container"],
        "heartbeat_advanced": alive_after["heartbeat"] > alive_before["heartbeat"],
        "permit": permit(),
    }
    old_epoch = next(
        t["epoch"]
        for t in manifest["maintenance_targets"]
        if t["uid"] == writer_targets()[0]["uid"]
    )
    receipt["late_requests"] = stale_dispatches(old_epoch, data.get("execution_id"))
    after = {"deployments": deployments(), "journal": journal()}
    receipt["no_write"] = {
        "deployments_unchanged": not any(
            written(before["deployments"], after["deployments"]).values()
        ),
        "journal_attempts_unchanged": before["journal"]["attempt_ids"]
        == after["journal"]["attempt_ids"],
        "before": before["deployments"],
        "after": after["deployments"],
    }
    # Readable and investigable while quarantined: resume collection only.
    dispatch_admin(
        "resume",
        "--class",
        "collection",
        "--reason",
        f"Investigate after restore {name}",
        "--evidence",
        f"local://release-restore/restore-{name}.json",
    )
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        ids = workload_ids(operator)
        fresh = incident(
            operator, oh, ids[STANDBY], f"Investigate quarantined standby after {name}"
        )
        run_view = wait_run(operator, fresh["incident_id"], fresh["run_id"])
        receipt["quarantined_investigation"] = {
            "run_state": run_view.get("state"),
            "sources": sorted(
                {
                    e["source_key"]
                    for e in run_view.get("evidence", [])
                    if e["coverage"]["status"] != "unavailable"
                }
            ),
        }
    receipt["permit_end"] = permit()
    receipt["deployments_end"] = deployments()
    print(write(f"restore-{name}.json", receipt))


def phase_fresh(args):
    data = bound_state()
    operator_password, _ = passwords()
    before = journal()
    targets = {t["uid"]: t for t in maintenance("status")["targets"]}
    manifest = read("manifest-d2.json")
    old_epoch = next(
        t["epoch"]
        for t in manifest["maintenance_targets"]
        if t["uid"] == writer_targets()[0]["uid"]
    )
    stale = stale_dispatches(old_epoch, None)
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        primary = data["primary_incident"]["incident_id"]
        old_executions = {
            e["id"] for e in operator.get(f"/api/incidents/{primary}/executions").json()
        }
        current = deployments()[writer_targets()[0]["name"]]["replicas"]
        response = propose(operator, oh, primary, 1 if current != 1 else 2, wait_cooldown=True)
        if response.status_code != 201:
            die(f"fresh proposal returned {response.status_code}: {response.text[:300]}")
        plan = response.json()
        decision = approve(operator, oh, plan)
        rows = wait_execution(operator, primary)
        new = [e for e in rows if e["id"] not in old_executions]
    after = journal()
    receipt = {
        "targets_enabled_before": {
            uid: {"enabled": t["enabled"], "recovery_mode": t["recovery_mode"]}
            for uid, t in targets.items()
        },
        "retired_epoch_while_enabled": stale,
        "plan_id": plan["id"],
        "decision_status": decision.status_code,
        "new_executions": [
            {k: e[k] for k in ("id", "state", "holds_lock")}
            | {"outcome": e["result"].get("outcome")}
            for e in new
        ],
        "journal_new_attempts": [i for i in after["attempt_ids"] if i not in before["attempt_ids"]],
        "restored_executions": executions(),
        "deployments": deployments(),
        "permit": permit(),
    }
    print(write("fresh-path.json", receipt))


def phase_late_writer(args):
    """Hold a real admitted patch past a real fence; Kubernetes must reject it afterwards."""
    data = bound_state()
    operator_password, _ = passwords()
    primary = writer_targets()[0]
    dc("stop", "worker")
    dc("cp", str(ROOT / "scripts/probe-late-writer.py"), "executor:/tmp/probe-late-writer.py")
    dc(
        "exec",
        "-T",
        "executor",
        "rm",
        "-f",
        "/tmp/e01-late-request.json",
        "/tmp/e01-late-proof.json",
        "/tmp/late-writer.log",
    )
    dc(
        "exec",
        "-T",
        "-d",
        "executor",
        "sh",
        "-c",
        "python /tmp/probe-late-writer.py > /tmp/late-writer.log 2>&1",
    )
    deadline = time.monotonic() + 60
    while "READY" not in out("exec", "-T", "executor", "cat", "/tmp/late-writer.log", check=False):
        if time.monotonic() > deadline:
            die("the late-writer probe did not become ready.")
        time.sleep(1)
    # The probe's own gateway start quarantined every target; reconcile through the root CLI.
    evidence = ["local://release-restore/late-writer.json"]
    opened = {}
    for target in writer_targets():
        opened[target["name"]] = maintenance(
            "enable_target",
            targets=[target["uid"]],
            evidence=evidence,
            reason="Reconciled before the controlled late-writer drill.",
        ).get("status")
    opened["exit_recovery_mode"] = maintenance(
        "exit_recovery_mode",
        evidence=evidence,
        reason="Open the target for the controlled late-writer drill.",
    ).get("status")
    if set(opened.values()) != {"applied"}:
        die(f"targets could not be opened for the drill: {opened}")
    before = deployments()
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        incident_id = data["primary_incident"]["incident_id"]
        current = before[primary["name"]]["replicas"]
        response = propose(operator, oh, incident_id, 1 if current != 1 else 2, wait_cooldown=True)
        if response.status_code != 201:
            die(f"drill proposal returned {response.status_code}: {response.text[:300]}")
        plan = response.json()
        decision = approve(operator, oh, plan)
        if decision.status_code != 200:
            die(f"drill approval returned {decision.status_code}: {decision.text[:300]}")
        rows = operator.get(f"/api/incidents/{incident_id}/executions").json()
        execution_id = rows[0]["id"]
        run(
            [
                *LAB.COMPOSE,
                "exec",
                "-T",
                "executor",
                "sh",
                "-c",
                "cat > /tmp/e01-late-request.json",
            ],
            stdin=json.dumps({"execution_id": execution_id}),
        )
        deadline = time.monotonic() + 180
        log = ""
        while time.monotonic() < deadline:
            log = out("exec", "-T", "executor", "cat", "/tmp/late-writer.log", check=False)
            if "PASS" in log or "Traceback" in log or "AssertionError" in log:
                break
            time.sleep(2)
        if "PASS" not in log:
            die(f"the late-writer probe did not pass: {log[-800:]}")
        proof = json.loads(out("exec", "-T", "executor", "cat", "/tmp/e01-late-proof.json"))
        execution = next(e for e in executions() if e["id"] == execution_id)
        status = next(t for t in maintenance("status")["targets"] if t["uid"] == primary["uid"])
    after = deployments()
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "worker")
    receipt = {
        "plan_id": plan["id"],
        "execution_id": execution_id,
        "dispatch_outcome": proof["dispatch"]["outcome"],
        "fence_excluded_attempt": execution_id
        in proof["maintenance"]["proofs"][primary["uid"]]["excluded_attempts"],
        "late_patch": proof["late_patch"],
        "replicas_before": before[primary["name"]]["replicas"],
        "replicas_after": after[primary["name"]]["replicas"],
        "retired_epoch_denied": proof["retired_epoch_denied"],
        "late_worker_preserved_unknown": proof["late_worker_preserved_unknown"],
        "execution_after": execution,
        "target_after_probe": {
            k: status[k] for k in ("enabled", "recovery_mode", "historical_unknown_count")
        },
        "journal_unknown": [
            a for a in maintenance("status")["unknown_attributions"] if a["id"] == execution_id
        ],
    }
    print(write("late-writer.json", receipt))


def phase_manual_exit(args):
    data = bound_state()
    operator_password, _ = passwords()
    receipt = {}
    primary = data["primary_incident"]["incident_id"]
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        current = deployments()[writer_targets()[0]["name"]]["replicas"]
        response = propose(operator, oh, primary, 1 if current != 1 else 2, wait_cooldown=True)
        if response.status_code != 201:
            die(f"pre-drill proposal returned {response.status_code}: {response.text[:300]}")
        pending = response.json()
    dispatch_admin(
        "enter-recovery",
        "--reason",
        "Authority loss drill",
        "--evidence",
        "local://release-restore/manual-exit.json",
    )
    entered = maintenance(
        "enter_recovery",
        reason="Authority loss drill",
        evidence=["local://release-restore/manual-exit.json"],
    )
    receipt["enter"] = entered.get("status")
    if entered.get("status") != "applied":
        die(f"quarantine failed before the drill: {entered}")
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        response = approve(operator, oh, pending)
        receipt["approval_under_quarantine"] = {
            "status": response.status_code,
            "code": code_of(response),
        }
    before = deployments()
    dc("rm", "-s", "-f", "executor")
    # Lose the pin: Compose recreates an empty identity volume; the journal is untouched.
    run(["docker", "volume", "rm", f"{LAB.PROJECT}_executor_identity"])
    started = dc("up", "-d", "--no-build", "--pull", "never", "executor", check=False)
    time.sleep(10)
    container = f"{LAB.PROJECT}-executor-1"
    inspected = run(
        ["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container],
        check=False,
    ).stdout.split()
    logs = run(["docker", "logs", "--tail", "20", container], check=False)
    receipt["executor"] = {
        "up_returncode": started.returncode,
        "status": inspected[0] if inspected else None,
        "exit_code": int(inspected[1]) if len(inspected) > 1 else None,
        "refused_missing_pin": PIN_MISSING in (logs.stdout + logs.stderr),
    }
    receipt["enable_attempt"] = maintenance(
        "enable_target",
        targets=[writer_targets()[0]["uid"]],
        reason="Attempt without authority",
        evidence=["local://release-restore/manual-exit.json"],
    )
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        response = propose(operator, oh, primary, 1, wait_cooldown=True)
        receipt["proposal_without_authority"] = {
            "status": response.status_code,
            "code": code_of(response),
        }
        receipt["reads"] = {"incident": operator.get(f"/api/incidents/{primary}").status_code}
    receipt["deployments_unchanged"] = not any(written(before, deployments()).values())
    receipt["manual_handoff"] = (
        "Writer authority is missing, so every target stays quarantined and product writes stay "
        "disabled. Handle the incident manually outside the product. Recovery requires the "
        "original executor journal and pin, or a deployment-owner reconciliation of the writer "
        "scope; a new authority is never initialized over retained history."
    )
    print(write("manual-exit.json", receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    sub.add_parser("prepare")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--label", required=True)
    reconcile.add_argument("--dispatch-exit", action="store_true")
    reconcile.add_argument("--prove-failure", metavar="TARGET_NAME")
    cut = sub.add_parser("cut")
    cut.add_argument("name", choices=["d1", "d2", "d3"])
    sub.add_parser("activity")
    restore = sub.add_parser("restore")
    restore.add_argument("name", choices=["d1", "d2", "d3"])
    sub.add_parser("fresh")
    sub.add_parser("late-writer")
    sub.add_parser("manual-exit")
    args = parser.parse_args()
    global LAB, ROOT, OUT, PRIVATE
    LAB = guard()
    ROOT = LAB.ROOT
    OUT = ROOT / "tmp/release-restore"
    PRIVATE = OUT / "private"
    {
        "prepare": phase_prepare,
        "reconcile": phase_reconcile,
        "cut": phase_cut,
        "activity": phase_activity,
        "restore": phase_restore,
        "fresh": phase_fresh,
        "late-writer": phase_late_writer,
        "manual-exit": phase_manual_exit,
    }[args.phase](args)


if __name__ == "__main__":
    main()
