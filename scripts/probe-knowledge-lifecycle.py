#!/usr/bin/env python3
"""Exercise Knowledge CLI/API recovery on an explicitly owned, empty local test lab.

DATABASE_URL stays in the process environment. The probe provisions disposable
accounts and retains its test database for inspection; it never truncates data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from cloudops.features.access.models import User
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeIndexAttempt,
)
from cloudops.shared.database import sessions

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# Fault injection is confined to this disposable CLI process, before capture or
# after the real activation commit. No production switch or endpoint is added.
BARRIER = """
import sys, time
from pathlib import Path
from cloudops.features.knowledge import indexing
from cloudops.entrypoints.cli import main
stage, marker, root = sys.argv[1:]
original = indexing._prepare if stage == 'capture' else indexing._activate
def paused(*args, **kwargs):
    result = original(*args, **kwargs) if stage == 'committed' else None
    Path(marker).write_text('ready', encoding='ascii')
    time.sleep(120)
    return result
if stage == 'capture':
    indexing._prepare = paused
else:
    indexing._activate = paused
sys.argv = ['cloudops', 'knowledge-index', '--root', root, '--json']
main()
"""


def checked(command, *, stdin=None):
    result = subprocess.run(
        command, cwd=ROOT, input=stdin, text=True, capture_output=True, timeout=90
    )
    if result.returncode:
        # Subprocess diagnostics may contain local configuration; retain no raw
        # stderr or environment values in the public receipt.
        raise RuntimeError(f"Probe subprocess failed (exit {result.returncode}).")
    return result.stdout.strip()


def cli(command, root=None, expected="indexed"):
    args = [PYTHON, "-m", "cloudops.entrypoints.cli", command, "--json"]
    if root is not None:
        args += ["--root", str(root)]
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=45)
    payload = json.loads(result.stdout)
    if command == "knowledge-index":
        assert payload["outcome"] == expected, payload["outcome"]
        assert result.returncode == (1 if expected in {"failed", "index_busy"} else 0)
    else:
        assert result.returncode == 0
    return payload


def wait_until(predicate, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}.")


def _listener_pids(port):
    if os.name != "nt" or port is None:
        return set()
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10
    )
    listeners = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) >= 5
            and fields[0].upper() == "TCP"
            and fields[-2].upper() == "LISTENING"
            and fields[1].rsplit(":", 1)[-1] == str(port)
        ):
            listeners.add(int(fields[-1]))
    return listeners


def stop(process, *, port=None):
    pids = _listener_pids(port)
    if process.poll() is None:
        pids.add(process.pid)
    if os.name == "nt":
        # A Windows Uvicorn launcher may exit while its worker still serves.
        # Include the PID bound to this probe's unique loopback port.
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
    elif process.poll() is None:
        process.terminate()
    if process.poll() is None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if port is not None:
        wait_until(lambda: not _listener_pids(port), "owned API listener shutdown", timeout=10)


def manifest(root, *, version="1", present=True, body="Original snapshot."):
    row = {
        "document_key": "probe-guide",
        "version": version,
        "title": "Probe guide",
        "source_path": "runbooks/probe-guide.md",
        "guide_kind": "diagnosis",
        "tags": ["probe"],
        "applicability": {"kind": "Deployment", "symptom_families": [], "recovery_templates": []},
        "provenance": {
            "source_revision": "6c8eea37e982f3dc6de2755e7a482eb16aaf1f16",
            "reviewed_at": "2026-11-12",
        },
    }
    (root / "runbooks").mkdir(exist_ok=True)
    (root / "runbooks/catalog.json").write_text(
        json.dumps({"schema_version": "RunbookCatalog/v1", "documents": [row] if present else []}),
        encoding="utf-8",
    )
    (root / row["source_path"]).write_bytes(f"# Probe guide\n\n{body}\n".encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if not __debug__:
        parser.error("This acceptance probe requires Python assertions; disable optimization.")
    parser.add_argument("--container", required=True, help="Owned PostgreSQL container to restart.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    url = make_url(os.environ["DATABASE_URL"])
    assert (
        url.host in {"localhost", "127.0.0.1"}
        and url.database.startswith("cloudops_pr64_")
        and url.database.endswith("_test")
    ), "Use a task-owned cloudops_pr64_*_test database over loopback."
    label = checked(
        [
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "cloudops.task" }}',
            args.container,
        ]
    )
    assert label == "pr64-review", "Container is not owned by this acceptance task."
    initial_binding = checked(["docker", "port", args.container, "5432/tcp"])
    bind_host, bind_port = initial_binding.rsplit(":", 1)
    if bind_host not in {"127.0.0.1", "localhost"} or url.port != int(bind_port):
        raise RuntimeError("DATABASE_URL must target the supplied container's loopback port.")
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeDocumentVersion)) == 0
        assert db.scalar(select(func.count()).select_from(User)) == 0

    receipt = {
        "schema": "Knowledge59LifecycleFollowup/v1",
        "proof_class": "Host CLI and HTTP API against task-owned PostgreSQL; controlled process barriers.",
        "source_commit": checked(["git", "rev-parse", "HEAD"]),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "postgres_image": checked(["docker", "inspect", "--format", "{{.Image}}", args.container]),
        "steps": {},
        "limits": [
            "Temporary corpus is synthetic; release corpus is indexed separately.",
            "Fault barriers wrap the normal CLI only inside owned subprocesses.",
            "No browser, semantic retrieval, Run pinning or Kubernetes recovery claim.",
        ],
    }
    steps = receipt["steps"]
    initial = cli("knowledge-status")
    assert initial["state"] == "not_indexed"
    steps["empty"] = initial["state"]
    release = cli("knowledge-index", ROOT)
    assert release["document_count"] == 8
    repeat = cli("knowledge-index", ROOT, "unchanged")
    assert repeat["generation_id"] == release["generation_id"]
    steps["release"] = release
    steps["release_repeat"] = repeat

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    os.environ.update(COOKIE_SECURE="false", WEB_ORIGIN=origin, COLLECTION_SOURCE_MODE="none")
    suffix = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    for role in ("viewer", "operator"):
        checked(
            [
                PYTHON,
                "-m",
                "cloudops.entrypoints.cli",
                "account",
                f"probe-{role}-{suffix}",
                "--role",
                role,
                "--password-stdin",
            ],
            stdin=password + "\n",
        )

    def start_api(log):
        api_environment = os.environ.copy()
        api_environment.pop("WEB_CONCURRENCY", None)
        api_environment.pop("UVICORN_RELOAD", None)
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "uvicorn",
                "cloudops.entrypoints.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=ROOT,
            env=api_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def ready():
            assert process.poll() is None, "Owned API exited before readiness."
            try:
                return (
                    httpx.get(origin + "/api/knowledge/index-status", timeout=1).status_code == 401
                )
            except httpx.TransportError:
                return False

        wait_until(ready, "owned API readiness")
        return process

    clients = []
    api = None
    try:
        with tempfile.TemporaryDirectory(prefix="cloudops-knowledge59-") as folder:
            temporary = Path(folder)
            corpus = temporary / "source"
            corpus.mkdir()
            with (temporary / "api.log").open("w", encoding="utf-8") as log:
                api = start_api(log)
                for role in ("viewer", "operator"):
                    client = httpx.Client(base_url=origin, timeout=45)
                    clients.append(client)
                    login = client.post(
                        "/api/login",
                        headers={"Origin": origin},
                        json={"username": f"probe-{role}-{suffix}", "password": password},
                    )
                    assert login.status_code == 200

                def get(path, expected=200, **params):
                    results = [client.get(path, params=params) for client in clients]
                    assert all(r.status_code == expected for r in results), [
                        r.status_code for r in results
                    ]
                    assert all(r.headers["cache-control"] == "no-store" for r in results)
                    return results[0].json()

                page = get("/api/knowledge/documents", limit=1)
                document_id = page["items"][0]["id"]
                document = get(f"/api/knowledge/documents/{document_id}")
                with sessions()() as db:
                    chunk_id = str(
                        db.scalar(
                            select(KnowledgeChunk.id)
                            .where(KnowledgeChunk.document_version_id == UUID(document_id))
                            .order_by(KnowledgeChunk.ordinal)
                        )
                    )
                chunk = get(f"/api/knowledge/chunks/{chunk_id}", document_version_id=document_id)
                get(
                    f"/api/knowledge/chunks/{chunk_id}",
                    expected=404,
                    document_version_id=str(uuid4()),
                )

                def assert_history():
                    assert get(f"/api/knowledge/documents/{document_id}") == document
                    assert (
                        get(f"/api/knowledge/chunks/{chunk_id}", document_version_id=document_id)
                        == chunk
                    )

                manifest(corpus)
                steps["fixture_v1"] = cli("knowledge-index", corpus)
                manifest(corpus, version="2", body="Reviewed new version.")
                steps["fixture_v2"] = cli("knowledge-index", corpus)
                manifest(corpus, present=False)
                removed = cli("knowledge-index", corpus)
                assert get("/api/knowledge/documents")["items"] == []
                assert_history()
                steps["removed"] = removed

                # Conflict against retired v1, not merely current membership.
                manifest(corpus, body="Forged original version.")
                steps["retired_conflict"] = cli("knowledge-index", corpus, "failed")
                status = get("/api/knowledge/index-status")
                assert status["state"] == "degraded"
                assert status["active_generation"]["id"] == removed["generation_id"]
                assert_history()
                manifest(corpus)
                steps["approved_old_snapshot"] = cli("knowledge-index", corpus, "reactivated")

                # Kill one writer before source capture, then another after the
                # actual DB activation committed but before CLI response.
                for stage in ("capture", "committed"):
                    manifest(
                        corpus,
                        version="3" if stage == "committed" else "2",
                        body="Committed retry."
                        if stage == "committed"
                        else "Reviewed new version.",
                    )
                    marker = temporary / f"{stage}.ready"
                    writer = subprocess.Popen(
                        [PYTHON, "-c", BARRIER, stage, str(marker), str(corpus)],
                        cwd=ROOT,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        wait_until(lambda: marker.exists() or writer.poll() is not None, stage)
                        assert marker.exists(), "Barrier writer exited before reaching stage."
                        if stage == "capture":
                            during = get("/api/knowledge/index-status")
                            assert during["state"] == "updating"
                            busy = cli("knowledge-index", temporary / "absent-source", "index_busy")
                            assert_history()
                            steps["competing_before_capture"] = busy
                    finally:
                        stop(writer)
                    status = get("/api/knowledge/index-status")
                    if stage == "capture":
                        assert status["state"] == "degraded"
                        assert (
                            status["active_generation"]["id"]
                            == steps["fixture_v1"]["generation_id"]
                        )
                    recovered = cli(
                        "knowledge-index",
                        corpus,
                        "unchanged" if stage == "committed" else "reactivated",
                    )
                    assert get("/api/knowledge/index-status")["state"] == "ready"
                    if stage == "capture":
                        with sessions()() as db:
                            assert (
                                db.scalar(
                                    select(func.count())
                                    .select_from(KnowledgeIndexAttempt)
                                    .where(KnowledgeIndexAttempt.outcome == "interrupted")
                                )
                                == 1
                            )
                    steps[f"killed_{stage}_retry"] = recovered

                # No automatic source rescan is needed for historical reads.
                (corpus / "runbooks/probe-guide.md").unlink()
                failed = cli("knowledge-index", corpus, "failed")
                assert get("/api/knowledge/index-status")["state"] == "degraded"
                assert_history()
                steps["missing_source"] = failed
                stop(api, port=port)
                api = start_api(log)
                assert_history()
                before = get("/api/knowledge/index-status")["active_generation"]
                checked(["docker", "restart", args.container])
                wait_until(
                    lambda: subprocess.run(
                        [
                            "docker",
                            "exec",
                            args.container,
                            "pg_isready",
                            "-U",
                            "cloudops",
                            "-d",
                            url.database,
                        ],
                        capture_output=True,
                    ).returncode
                    == 0,
                    "owned PostgreSQL database recovery",
                    timeout=60,
                )
                published = checked(["docker", "port", args.container, "5432/tcp"])
                bind_host, bind_port = published.rsplit(":", 1)
                if bind_host not in {"127.0.0.1", "localhost"}:
                    raise RuntimeError("Owned PostgreSQL container is not bound to loopback.")
                url = url.set(host="127.0.0.1", port=int(bind_port))
                os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
                # Docker may assign a fresh host port to a dynamic binding on
                # restart; start the owned API against that verified mapping.
                stop(api, port=port)
                api = start_api(log)
                assert_history()
                after = get("/api/knowledge/index-status")
                assert after["active_generation"] == before
                assert after["state"] == "degraded"
                steps["api_database_restart"] = {
                    "same_generation": before["id"],
                    "document_version_id": document_id,
                    "document_sha256": document["content_sha256"],
                    "chunk_id": chunk_id,
                    "chunk_sha256": chunk["content_sha256"],
                    "same_bytes_and_metadata": True,
                    "source_missing": True,
                    "both_roles": True,
                }
                steps["final_explicit_retry"] = cli("knowledge-index", ROOT, "reactivated")
                assert get("/api/knowledge/index-status")["state"] == "ready"
    finally:
        if api is not None:
            stop(api, port=port)
        for client in clients:
            client.close()
    receipt["cleanup"] = {
        "owned_api_stopped": True,
        "temporary_sources_removed": True,
        "database_retained": True,
    }
    Path(args.output).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print("Knowledge lifecycle acceptance passed; sanitized receipt written.")


if __name__ == "__main__":
    main()
