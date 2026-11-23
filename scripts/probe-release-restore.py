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
JOBS_DIGEST = (
    "select md5(coalesce(string_agg(t::text, chr(10) order by t::text), %s)) from jobs t" % "''"
)
ORIGIN = "https://localhost:8443"
STANDBY = "checkout-standby"
KEY_TABLES = (
    "incidents",
    "incident_activity",
    "investigation_runs",
    "evidence",
    "action_plans",
    "recovery_executions",
    "jobs",
)
# Rows that trusted restore cleanup or bootstrap legitimately changes or removes.
CLEANUP_TABLES = {
    "sessions": "restored sessions revoked",
    "plan_reviews": "restored review receipts retired",
    "jobs": "restored recovery jobs retired",
    "job_attempts": "running restored attempts closed",
    "recovery_executions": "active restored executions retired",
    "action_plans": "proposed and retired approved plans invalidated",
    "recovery_verifications": "restored verification authority retired",
    "workloads": "bootstrap scope synchronization counters",
    "workload_scope_state": "bootstrap scope synchronization counters",
    "worker_heartbeats": "live worker heartbeat",
}


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
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def passwords():
    lines = [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
    if not all(lines):
        die("provide the Operator and Viewer passwords on stdin.")
    return lines


# ---------- identities and fingerprints ----------


def database_identity():
    if sql("select current_database()").strip() != RESTORE_DB:
        die("connected database is not the owned restore database.")
    return {
        "database": RESTORE_DB,
        "server_version": sql("show server_version").strip(),
        "alembic_head": sql("select version_num from alembic_version").strip(),
        "observed_at": sql("select now()").strip(),
        "txid": sql("select pg_current_xact_id()::text").strip(),
    }


def fingerprint():
    tables = sql(
        "select table_name || '|' || bool_or(column_name = 'id') from information_schema.columns "
        "where table_schema = 'public' group by table_name order by table_name"
    ).split()
    parts = []
    for item in tables:
        table, has_id = item.split("|")
        ids = (
            "md5(coalesce(string_agg(id::text, ',' order by id::text), ''))"
            if has_id == "true"
            else "''"
        )
        listing = (
            "coalesce(json_agg(id::text order by id::text), '[]')::text"
            if table in KEY_TABLES and has_id == "true"
            else "'[]'"
        )
        parts.append(
            f"select '{table}' || chr(9) || count(*) || chr(9) || "
            f"md5(coalesce(string_agg(t::text, chr(10) order by t::text), '')) || chr(9) || "
            f'{ids} || chr(9) || {listing} from public."{table}" t'
        )
    rows = sql(" union all ".join(parts)).strip().splitlines()
    result = {}
    for row in rows:
        table, count, digest, ids, listing = row.split("\t")
        result[table] = {"rows": int(count), "md5": digest, "ids_md5": ids}
        if table in KEY_TABLES:
            result[table]["ids"] = json.loads(listing)
    return result


def evidence_digests():
    rows = sql("select id::text || ' ' || coalesce(sha256, '') from evidence order by id").split(
        "\n"
    )
    return dict(line.split(" ", 1) for line in rows if line.strip())


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


def dispatch_admin(*args):
    return (
        jsonout("run", "--rm", "-T", "dispatch-admin", *args)
        if args[0] == "status"
        else (dc("run", "--rm", "-T", "dispatch-admin", *args).stdout)
    )


def permit():
    status = dispatch_admin("status")
    p = status["permit"]
    return (
        None
        if p is None
        else {
            "authority_id": p["authority_id"],
            "generation": p["generation"],
            "mode": p["mode"],
            "allowed_job_kinds": p["allowed_job_kinds"],
            "denial": status.get("denial"),
        }
    )


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


# ---------- HTTPS client ----------


def client():
    import httpx

    ca = OUT / "ca.crt"
    return httpx.Client(
        base_url=ORIGIN,
        verify=ssl.create_default_context(cafile=str(ca)),
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
    while time.monotonic() < deadline:
        run_view = session.get(f"/api/incidents/{incident_id}/runs/{run_id}").json()
        if run_view.get("state") not in ("queued", "collecting"):
            return run_view
        time.sleep(3)
    return run_view


def propose(session, headers, incident_id, replicas):
    return session.post(
        f"/api/incidents/{incident_id}/plans",
        json={"replicas": replicas, "rationale": "Release restore qualification scale"},
        headers=headers,
    )


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


# ---------- phases ----------


def phase_prepare(args):
    if LAB.PROJECT in ("cloudops-e01-https", LAB.OWNED_PROJECT):
        die("use a new disposable project.")
    config = json.loads(out("config", "--format", "json"))
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
    operator_password, viewer_password = passwords()
    for user, role, password in (
        ("operator", "operator", operator_password),
        ("viewer", "viewer", viewer_password),
    ):
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
            role,
            "--password-stdin",
            stdin=password + "\n",
        )
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    dc("cp", "https:/data/caddy/pki/authorities/local/root.crt", str(OUT / "ca.crt"))
    receipt = {
        "project": LAB.PROJECT,
        "bundle": bundle_identity(),
        "database": database_identity(),
        "targets": writer_targets(),
        "scope_sha256": scope_digests(),
        "permit": permit(),
        "maintenance": maintenance("status"),
        "journal": journal(),
        "deployments": deployments(),
        "ca_sha256": hashlib.sha256((OUT / "ca.crt").read_bytes()).hexdigest(),
    }
    save_state({"targets": writer_targets()})
    print(write("prepare.json", receipt))


def phase_reconcile(args):
    label = args.label
    before = {
        "deployments": deployments(),
        "journal": journal(),
        "maintenance": maintenance("status"),
    }
    path = write(f"reconcile-{label}.json", {"observed": before})
    evidence = [f"local://release-restore/{path.name}"]
    results = {}
    for target in writer_targets():
        results[f"enable_{target['name']}"] = maintenance(
            "enable_target",
            targets=[target["uid"]],
            evidence=evidence,
            reason=f"Reconciled current state ({label}); fence and health proof required.",
        )
    results["exit_recovery_mode"] = maintenance(
        "exit_recovery_mode", evidence=evidence, reason=f"All targets reconciled ({label})."
    )
    if args.dispatch_exit:
        dispatch_admin(
            "exit-recovery",
            "--reason",
            f"Restored scope reconciled ({label})",
            "--evidence",
            evidence[0],
        )
    after = {"deployments": deployments(), "journal": journal(), "permit": permit()}
    write(f"reconcile-{label}.json", {"observed": before, "results": results, "after": after})
    for name, value in results.items():
        if value.get("status") != "applied":
            die(f"{name} was not applied: {value.get('code')} {value.get('reason')}")
    print(path)


def cut_manifest(name, extra):
    dump = PRIVATE / f"{name}.dump"
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = database_identity()
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
    dump.write_bytes(data)
    os.chmod(dump, 0o600)
    manifest = {
        "schema": "ReleaseRestoreManifest/v1",
        "cutoff": name,
        "project": LAB.PROJECT,
        "bundle": bundle_identity(),
        "database": started,
        "dump": {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "format": "pg_dump -Fc",
        },
        "fingerprint": fingerprint(),
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
    operator_password, _ = passwords()
    data = state()
    with client() as session:
        headers = login(session, "operator", operator_password)
        ids = workload_ids(session)
        primary = writer_targets()[0]["name"]
        if args.name == "d1":
            main = incident(session, headers, ids[primary], "Release restore primary")
            standby = incident(session, headers, ids[STANDBY], "Release restore standby")
            wait_run(session, main["incident_id"], main["run_id"])
            replicas = 2 if deployments()[primary]["replicas"] == 1 else 1
            response = propose(session, headers, main["incident_id"], replicas)
            if response.status_code != 201:
                die(f"proposal returned {response.status_code}: {response.text[:300]}")
            data.update(
                primary_incident=main,
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
    operator_password, _ = passwords()
    data = state()
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
            "database": database_identity(),
            "fingerprint": fingerprint(),
            "evidence_sha256": evidence_digests(),
            "journal": journal(),
            "deployments": deployments(),
        },
    )
    print(OUT / "live.json")


def compare(manifest, restored, live):
    tables = {}
    for table, expected in manifest["fingerprint"].items():
        got = restored.get(table)
        if got is None:
            tables[table] = {"result": "missing"}
            continue
        if got["md5"] == expected["md5"]:
            result = "identical"
        elif table in ("sessions", "plan_reviews", "worker_heartbeats"):
            result = "changed by " + CLEANUP_TABLES[table]
        elif got["ids_md5"] == expected["ids_md5"] and table in CLEANUP_TABLES:
            result = "identifiers retained; changed by " + CLEANUP_TABLES[table]
        else:
            result = "unexpected change"
        tables[table] = {
            "result": result,
            "rows_cutoff": expected["rows"],
            "rows_restored": got["rows"],
        }
    loss = {}
    for table in KEY_TABLES:
        now = set(restored.get(table, {}).get("ids", []))
        then = set(live["fingerprint"].get(table, {}).get("ids", []))
        loss[table] = {"lost_after_cutoff": sorted(then - now), "count": len(then - now)}
    return tables, loss


def phase_restore(args):
    name = args.name
    manifest = read(f"manifest-{name}.json") or die(f"no manifest for {name}")
    live = read("live.json") or die("record post-cutoff activity first")
    data = state()
    operator_password, viewer_password = passwords()
    receipt = {"cutoff": name, "dump_sha256": manifest["dump"]["sha256"]}
    dump = PRIVATE / f"{name}.dump"
    if hashlib.sha256(dump.read_bytes()).hexdigest() != manifest["dump"]["sha256"]:
        die("dump does not match its manifest.")
    receipt["permit_before"] = permit()
    dispatch_admin(
        "enter-recovery",
        "--reason",
        f"Restore {name}",
        "--evidence",
        f"local://release-restore/manifest-{name}.json",
    )
    enter = maintenance(
        "enter_recovery",
        reason=f"Restore {name}",
        evidence=[f"local://release-restore/manifest-{name}.json"],
    )
    receipt["maintenance_enter"] = {k: enter.get(k) for k in ("status", "request_id", "code")}
    dc("stop", "worker", "api", "https", "observer")
    if sql("select current_database()").strip() != RESTORE_DB:
        die("refusing to restore outside the owned restore database.")
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
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "api", "https", timeout=600)
    receipt["permit_after"] = permit()
    receipt["database"] = database_identity()
    restored = fingerprint()
    tables, loss = compare(manifest, restored, live)
    receipt["tables"] = tables
    receipt["post_cutoff_loss"] = loss
    receipt["evidence_bytes_equal"] = evidence_digests() == manifest["evidence_sha256"]
    status = maintenance("status")
    receipt["targets_after"] = [
        {k: t[k] for k in ("uid", "enabled", "recovery_mode", "historical_unknown_count")}
        for t in status["targets"]
    ]
    receipt["unknown_attributions"] = status["unknown_attributions"]
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
        plan = operator.get(f"/api/incidents/{primary}/plans").json()
        receipt["restored_plans"] = [{k: p[k] for k in ("id", "state")} for p in plan]
        old = next((p for p in plan if p["id"] == data["plan"]["id"]), None)
        if old is not None:
            response = approve(operator, oh, old)
            receipt["old_plan_approval"] = {
                "status": response.status_code,
                "code": response.json().get("error", {}).get("code"),
            }
        receipt["executions"] = [
            {k: e[k] for k in ("id", "state", "holds_lock")}
            | {"outcome": e["result"].get("outcome")}
            for e in operator.get(f"/api/incidents/{primary}/executions").json()
        ]
        proposals = {}
        for key, inc in (
            ("primary", primary),
            ("standby", data["standby_incident"]["incident_id"]),
        ):
            response = propose(operator, oh, inc, 1)
            proposals[key] = {
                "status": response.status_code,
                "code": response.json().get("error", {}).get("code"),
            }
        receipt["new_proposals_while_quarantined"] = proposals
        receipt["maintenance_paths"] = {
            path: operator.post(path, json={}, headers=oh).status_code
            for path in (
                "/api/maintenance/enable_target",
                "/api/executor/exit_recovery_mode",
                "/api/targets/enable",
            )
        }
    # Restart and replay attempts must not write.
    before = {"deployments": deployments(), "journal": journal()}
    dc("restart", "executor")
    dc("up", "-d", "--no-build", "--pull", "never", "--wait", "executor", timeout=300)
    replay = maintenance(
        "enter_recovery",
        reason=f"Restore {name}",
        evidence=[f"local://release-restore/manifest-{name}.json"],
        request_id=enter.get("request_id"),
    )
    receipt["maintenance_replay"] = {
        "replayed": replay.get("replayed"),
        "status": replay.get("status"),
    }
    receipt["cleanup_replay"] = jsonout("run", "--rm", "--no-deps", "restore-admin", timeout=300)
    jobs_before = sql(JOBS_DIGEST)
    attempts_before = sql("select count(*) from job_attempts").strip()
    dc("up", "-d", "--no-build", "--pull", "never", "worker")
    time.sleep(25)
    receipt["old_worker"] = {
        "jobs_unchanged": sql(JOBS_DIGEST) == jobs_before,
        "attempts_unchanged": sql("select count(*) from job_attempts").strip() == attempts_before,
        "permit": permit(),
    }
    receipt["late_requests"] = late_requests(manifest, data)
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


def late_requests(manifest, data):
    """Send stale dispatches with the worker's own key; the executor must refuse without writing."""
    primary = manifest["targets"][0]["uid"]
    old_epoch = next(t["epoch"] for t in manifest["maintenance_targets"] if t["uid"] == primary)
    code = f"""
import json, os, uuid, datetime, urllib.request, urllib.error
key = open('/run/cloudops-dispatch/key').read().strip()
def send(body):
    request = urllib.request.Request(os.environ['EXECUTOR_URL'] + '/dispatch', data=json.dumps(body).encode(),
        headers={{'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {{'status': response.status, 'body': json.loads(response.read())}}
    except urllib.error.HTTPError as error:
        return {{'status': error.code, 'code': json.loads(error.read()).get('error', {{}}).get('code')}}
base = dict(uid={primary!r}, epoch={old_epoch!r}, resource_version='1', replicas=1, before={{}},
    plan_digest='0' * 64, expires_at=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=4)).isoformat())
print(json.dumps({{
    'old_epoch_new_identity': send(dict(base, execution_id=str(uuid.uuid4()))),
    'restored_identity_changed_content': send(dict(base, execution_id={data.get("execution_id")!r} or str(uuid.uuid4()))),
}}))
"""
    return json.loads(out("exec", "-T", "worker", "python", "-c", code).strip().splitlines()[-1])


def phase_fresh(args):
    operator_password, _ = passwords()
    data = state()
    before = journal()
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        primary = data["primary_incident"]["incident_id"]
        old_executions = {
            e["id"] for e in operator.get(f"/api/incidents/{primary}/executions").json()
        }
        current = deployments()[writer_targets()[0]["name"]]["replicas"]
        response = propose(operator, oh, primary, 1 if current != 1 else 2)
        if response.status_code != 201:
            die(f"fresh proposal returned {response.status_code}: {response.text[:300]}")
        plan = response.json()
        decision = approve(operator, oh, plan)
        rows = wait_execution(operator, primary)
        new = [e for e in rows if e["id"] not in old_executions]
    after = journal()
    receipt = {
        "plan_id": plan["id"],
        "decision_status": decision.status_code,
        "new_executions": [
            {k: e[k] for k in ("id", "state", "holds_lock")}
            | {"outcome": e["result"].get("outcome")}
            for e in new
        ],
        "journal_new_attempts": [i for i in after["attempt_ids"] if i not in before["attempt_ids"]],
        "deployments": deployments(),
        "permit": permit(),
    }
    print(write("fresh-path.json", receipt))


def phase_manual_exit(args):
    operator_password, _ = passwords()
    data = state()
    receipt = {}
    dispatch_admin(
        "enter-recovery",
        "--reason",
        "Authority loss drill",
        "--evidence",
        "local://release-restore/manual-exit.json",
    )
    receipt["enter"] = maintenance(
        "enter_recovery",
        reason="Authority loss drill",
        evidence=["local://release-restore/manual-exit.json"],
    ).get("status")
    before = deployments()
    dc("rm", "-s", "-f", "executor")
    volume = f"{LAB.PROJECT}_executor_identity"
    # Lose the pin: Compose recreates an empty identity volume; the journal is untouched.
    run(["docker", "volume", "rm", volume])
    started = dc("up", "-d", "--no-build", "--pull", "never", "executor", check=False)
    time.sleep(10)
    state_text = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}} {{.RestartCount}}",
            f"{LAB.PROJECT}-executor-1",
        ],
        check=False,
    ).stdout.strip()
    logs = run(["docker", "logs", "--tail", "20", f"{LAB.PROJECT}-executor-1"], check=False)
    receipt["executor"] = {
        "up_returncode": started.returncode,
        "state": state_text,
        "refused_missing_pin": "identity" in (logs.stdout + logs.stderr).lower()
        or "authority" in (logs.stdout + logs.stderr).lower(),
    }
    receipt["enable_attempt"] = maintenance(
        "enable_target",
        targets=[writer_targets()[0]["uid"]],
        reason="Attempt without authority",
        evidence=["local://release-restore/manual-exit.json"],
    )
    with client() as operator:
        oh = login(operator, "operator", operator_password)
        primary = data["primary_incident"]["incident_id"]
        response = propose(operator, oh, primary, 1)
        receipt["proposal_without_authority"] = {
            "status": response.status_code,
            "code": response.json().get("error", {}).get("code"),
        }
        receipt["reads"] = {"incident": operator.get(f"/api/incidents/{primary}").status_code}
    receipt["deployments_unchanged"] = not any(written(before, deployments()).values())
    receipt["manual_handoff"] = (
        "Writer authority is missing, so every target stays quarantined and product writes stay disabled. "
        "Handle the incident manually outside the product. Restore requires the original executor journal "
        "and pin or a deployment-owner reconciliation; a new authority is never initialized over retained history."
    )
    print(write("manual-exit.json", receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    sub.add_parser("prepare")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--label", required=True)
    reconcile.add_argument("--dispatch-exit", action="store_true")
    cut = sub.add_parser("cut")
    cut.add_argument("name", choices=["d1", "d2", "d3"])
    sub.add_parser("activity")
    restore = sub.add_parser("restore")
    restore.add_argument("name", choices=["d1", "d2", "d3"])
    sub.add_parser("fresh")
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
        "manual-exit": phase_manual_exit,
    }[args.phase](args)


if __name__ == "__main__":
    main()
