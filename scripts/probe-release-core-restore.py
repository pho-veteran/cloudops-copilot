"""Qualify the core-only (no executor) release restore path in a disposable project.

Run from a verified release install directory on Linux with CLOUDOPS_CORE_PROJECT (a new
project), CLOUDOPS_RELEASE_BUNDLE and CLOUDOPS_HOST_ENV set. Passwords are read from stdin
(Operator, then Viewer). Only the project's own cloudops_restore_test database is restored.
Receipts are written to tmp/release-core-restore/.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "compose.yaml",
    "compose.https.yaml",
    "compose.release.yaml",
    "compose.release-core-restore.yaml",
)
RESERVED = {"cloudops-local", "cloudops-e01-https", "cloudops-local-testing-lab"}


def load_driver():
    spec = importlib.util.spec_from_file_location(
        "release_restore_driver", ROOT / "scripts/probe-release-restore.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup():
    if os.name != "posix":
        raise SystemExit("release core restore: run from a Linux host or WSL.")
    project = os.environ.get("CLOUDOPS_CORE_PROJECT", "")
    bundle, host = os.environ.get("CLOUDOPS_RELEASE_BUNDLE"), os.environ.get("CLOUDOPS_HOST_ENV")
    if not project or project in RESERVED or not bundle or not host:
        raise SystemExit(
            "release core restore: set a new CLOUDOPS_CORE_PROJECT, CLOUDOPS_RELEASE_BUNDLE "
            "and CLOUDOPS_HOST_ENV."
        )
    if [
        name
        for name in os.environ
        if name in ("RELEASE_BACKEND_IMAGE", "RELEASE_DB_IMAGE", "RELEASE_HTTPS_IMAGE")
    ]:
        raise SystemExit("release core restore: unset inherited RELEASE_* image variables.")
    os.umask(0o077)
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(Path(host).resolve()),
        "--env-file",
        str(Path(bundle).resolve() / "images.env"),
        "-p",
        project,
    ]
    for name in FILES:
        compose += ["-f", name]
    driver = load_driver()
    driver.LAB = SimpleNamespace(COMPOSE=compose, PROJECT=project, LAB_DIR=None)
    driver.ROOT = ROOT
    driver.OUT = ROOT / "tmp/release-core-restore"
    driver.PRIVATE = driver.OUT / "private"
    return driver, project


def main():
    d, project = setup()
    if d.run(
        ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}"]
    ).stdout.strip():
        d.die("use a new project with no containers.")
    config = json.loads(d.out("--profile", "*", "config", "--format", "json"))
    if "executor" in config["services"]:
        d.die("the core drill must not include an executor.")
    for name, service in config["services"].items():
        url = (service.get("environment") or {}).get("DATABASE_URL")
        if url and not url.endswith("/" + d.RESTORE_DB):
            d.die(f"{name} would connect outside the owned restore database.")
    operator_password, viewer_password = d.passwords()
    receipt = {"project": project, "bundle": d.bundle_identity()}
    d.dc(
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
        "https",
        timeout=900,
    )
    receipt["connections_use_restore_database"] = d.connections_owned(("api", "worker"))
    for user, password in (("operator", operator_password), ("viewer", viewer_password)):
        d.dc(
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
    d.OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.dc("cp", "https:/data/caddy/pki/authorities/local/root.crt", str(d.OUT / "ca.crt"))
    d.dispatch_admin(
        "bootstrap",
        "--fresh-install",
        "--reason",
        "Core restore drill",
        "--evidence",
        "local://release-core-restore",
    )
    with d.client() as session:
        headers = d.login(session, "operator", operator_password)
        workload = session.get("/api/workloads").json()["items"][0]["id"]
        first = d.incident(session, headers, workload, "Core restore record")
        session.post(
            f"/api/incidents/{first['incident_id']}/notes",
            json={"text": "Before the cutoff"},
            headers=headers,
        )
        d.wait_run(session, first["incident_id"], first["run_id"])
        cookie = session.cookies.get("cloudops_session")
    # Cutoff: consistent dump with table digests checked before and after.
    d.PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = {"database": d.database_identity(), "fingerprint": d.fingerprint()}
    dump = d.dc(
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "cloudops",
        "-d",
        d.RESTORE_DB,
        "-Fc",
        stdin=b"",
        timeout=600,
    ).stdout
    after = {"database": d.database_identity(), "fingerprint": d.fingerprint()}
    moved = sorted(
        t
        for t in before["fingerprint"]
        if t not in d.VOLATILE_TABLES
        and before["fingerprint"][t]["md5"] != after["fingerprint"][t]["md5"]
    )
    if moved:
        d.die(f"records changed while the backup ran: {moved}")
    manifest = {
        "schema": "ReleaseRestoreManifest/v1",
        "cutoff": "core",
        "project": project,
        "bundle": receipt["bundle"],
        "database": before["database"],
        "database_after_dump": after["database"],
        "dump": {"sha256": hashlib.sha256(dump).hexdigest(), "bytes": len(dump)},
        "fingerprint": before["fingerprint"],
        "evidence_sha256": d.evidence_digests(),
        "permit": d.permit(),
    }
    d.write("manifest-core.json", manifest)
    with d.client() as session:
        headers = d.login(session, "operator", operator_password)
        later = d.incident(session, headers, workload, "Core post-cutoff record")
        d.wait_run(session, later["incident_id"], later["run_id"])
    live = {"fingerprint": d.fingerprint()}
    # Restore.
    receipt["permit_before"] = d.permit()
    d.dispatch_admin(
        "enter-recovery",
        "--reason",
        "Core restore",
        "--evidence",
        "local://release-core-restore/manifest-core.json",
    )
    d.dc("stop", "worker", "api", "https", "observer")
    d.dc(
        "exec",
        "-T",
        "db",
        "pg_restore",
        "-U",
        "cloudops",
        "-d",
        d.RESTORE_DB,
        "--clean",
        "--if-exists",
        "--no-owner",
        stdin=dump,
        timeout=600,
    )
    d.dc("run", "--rm", "--no-deps", "migrate", timeout=600)
    receipt["revoke_all_sessions"] = d.out(
        "run",
        "--rm",
        "--no-deps",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        "revoke-all-sessions",
    ).strip()[-200:]
    receipt["diagnosis_cleanup"] = d.out(
        "run", "--rm", "--no-deps", "api", "python", "-m", "cloudops.entrypoints.diagnosis_recovery"
    ).strip()[-300:]
    restored = d.fingerprint()
    tables, rows = d.compare(manifest, restored, live)
    receipt["tables"] = tables
    receipt["rows"] = rows
    receipt["evidence_bytes_equal"] = d.evidence_digests() == manifest["evidence_sha256"]
    d.dc("up", "-d", "--no-build", "--pull", "never", "--wait", "api", "https", timeout=600)
    receipt["permit_after"] = d.permit()
    with d.client() as session:
        receipt["restored_session"] = session.get(
            "/api/session", headers={"Cookie": f"cloudops_session={cookie}"}
        ).status_code
    with d.client() as operator, d.client() as viewer:
        oh = d.login(operator, "operator", operator_password)
        d.login(viewer, "viewer", viewer_password)
        receipt["reads"] = {
            "incident": viewer.get(f"/api/incidents/{first['incident_id']}").status_code,
            "run": viewer.get(
                f"/api/incidents/{first['incident_id']}/runs/{first['run_id']}"
            ).status_code,
        }
        response = d.propose(operator, oh, first["incident_id"], 2)
        receipt["proposal_without_executor"] = {
            "status": response.status_code,
            "code": d.code_of(response),
        }
        health = operator.get("/api/health").json()
        receipt["executor_health"] = [
            {k: o.get(k) for k in ("component", "status", "reason_code")}
            for o in health["observations"]
            if o["component"] == "executor"
        ]
    jobs_before = d.sql(d.digest_query("jobs"))
    attempts_before = d.sql(d.digest_query("job_attempts"))
    beat_before = d.worker_alive()["heartbeat"]
    d.dc("up", "-d", "--no-build", "--pull", "never", "worker")
    time.sleep(25)
    alive = d.worker_alive()
    receipt["old_worker"] = {
        "jobs_unchanged": d.sql(d.digest_query("jobs")) == jobs_before,
        "attempts_unchanged": d.sql(d.digest_query("job_attempts")) == attempts_before,
        "container": alive["container"],
        "heartbeat_advanced": alive["heartbeat"] > beat_before,
    }
    d.dispatch_admin(
        "exit-recovery",
        "--reason",
        "Core restore reconciled",
        "--evidence",
        "local://release-core-restore/core-restore.json",
    )
    receipt["permit_end"] = d.permit()
    with d.client() as session:
        headers = d.login(session, "operator", operator_password)
        fresh = d.incident(session, headers, workload, "Core record after restore")
        view = d.wait_run(session, fresh["incident_id"], fresh["run_id"])
        receipt["fresh_admission"] = {"run_state": view.get("state")}
    print(d.write("core-restore.json", receipt))


if __name__ == "__main__":
    main()
