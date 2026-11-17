"""Owned PostgreSQL restore and real three-class worker dispatch, with loopback provider fixtures.

Requires Docker and the built cloudops-local-backend image. Only a fresh project
whose name starts with cloudops-e04-restore- is accepted. No host cluster is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "cloudops_e04_restore_test"


def inside(stage, retired):
    from datetime import datetime, timedelta, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import tempfile
    import threading
    from sqlalchemy import select
    from cloudops.shared.database import sessions
    from cloudops.shared.jobs import Job
    from cloudops.features.investigations.models import InvestigationRun
    from cloudops.features.recommendations.service import prepare_report
    from cloudops.features.recommendations.krr_protocol import digest
    from cloudops.features.recovery.models import ActionPlan, Execution

    def snapshot():
        with sessions()() as db:
            return {
                j.kind: {
                    "id": str(j.id),
                    "subject_id": str(j.subject_id),
                    "state": j.state,
                    "attempts": j.attempts,
                }
                for j in db.scalars(select(Job).order_by(Job.kind))
            }

    os.environ["KRR_RUNNER_IMAGE_DIGEST"] = "sha256:" + "f" * 64

    if stage == "seed":
        from alembic import command
        from alembic.config import Config
        from cloudops.features.access.service import provision
        from cloudops.features.access.models import User
        from cloudops.features.workloads.service import sync_config
        from cloudops.features.workloads.models import Workload
        from cloudops.features.incidents.models import Incident

        command.upgrade(Config("/workspace/services/backend/alembic.ini"), "head")
        with sessions()() as db:
            provision(db, "restore-operator", "disposable-restore-only", "operator")
            sync_config(db)
        with sessions()() as db, db.begin():
            actor = db.scalar(select(User.id))
            workload = db.scalar(select(Workload).where(Workload.active))
            target = Workload(
                cluster="restore-fixture",
                namespace="demo",
                name="target",
                uid="restored-target-uid",
                source="live_kubernetes",
            )
            db.add(target)
            db.flush()
            a = Incident(
                workload_id=workload.id,
                title="Restore collection fixture",
                description="Explicit current-record fixture",
                created_by=actor,
            )
            b = Incident(
                workload_id=target.id,
                title="Restore approval fixture",
                description="No real target or Kubernetes writer",
                created_by=actor,
            )
            db.add_all([a, b])
            db.flush()
            now = datetime.now(timezone.utc)
            run = InvestigationRun(
                incident_id=a.id,
                requested_by=actor,
                state="queued",
                reason="Restore drill",
                question="Collect after restore",
                question_source="operator",
                window_start=now - timedelta(minutes=1),
                window_end=now,
                window_source="explicit",
            )
            report = prepare_report(db, workload, actor)
            report.input_identity = {
                **report.input_identity,
                "cluster_fingerprint": "a" * 64,
                "template_digest": "b" * 64,
                "resources_digest": "c" * 64,
                "pod_template_digest": "d" * 64,
                "containers": ["app"],
            }
            plan = ActionPlan(
                incident_id=b.id,
                workload_id=target.id,
                created_by=actor,
                state="approved",
                rationale="DB-seeded current approval fixture",
                target_epoch="before-restore",
                digest="a" * 64,
                expires_at=now + timedelta(hours=2),
                content={
                    "workload": {"uid": target.uid},
                    "replicas": 1,
                    "resource_version": "fixture-rv",
                    "before": {"replicas": 1},
                },
            )
            db.add_all([run, report, plan])
            db.flush()
            execution = Execution(plan_id=plan.id, workload_id=target.id)
            db.add(execution)
            db.flush()
            db.add_all(
                [
                    Job(kind="development_collection_v1", subject_id=run.id),
                    Job(kind="recommendation_report_v1", subject_id=report.id),
                    Job(kind="recovery_execution_v1", subject_id=execution.id),
                ]
            )
        return {"jobs": snapshot()}

    from cloudops.entrypoints import worker
    from cloudops.features.investigations import collector

    calls = []
    original = collector.REGISTERED_ADAPTERS["development_fixture"]

    def counted(*args, **kwargs):
        calls.append("collection")
        return original(*args, **kwargs)

    collector.REGISTERED_ADAPTERS["development_fixture"] = counted

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers.get("Authorization") == "Bearer local-restore-fixture"
            if self.path == "/report":
                calls.append("recommendation")
                status, result = (
                    200,
                    {
                        "state": "unavailable",
                        "protocol_version": 1,
                        "request_sha256": digest(body),
                        "reason": "runner_source_unavailable",
                        "tool_version": "1.31.1",
                        "image_digest": body["profile"]["image_digest"],
                        "invocation_started_at": None,
                        "invocation_finished_at": None,
                        "tool_exit_code": None,
                        "timed_out": False,
                        "output_sha256": None,
                        "scope_before": None,
                        "scope_after": None,
                        "containers": [],
                    },
                )
            elif self.path == "/dispatch":
                calls.append("recovery_execution")
                assert body["epoch"] == "before-restore"
                status = 409 if retired else 200
                result = {
                    "outcome": "denied",
                    "verification": "not_performed",
                    "reason": "Controlled gateway fixture; no write capability",
                }
            else:
                raise AssertionError("Unexpected provider path")
            encoded = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "key"
            key.write_text("local-restore-fixture")
            os.environ.update(
                KRR_RUNNER_URL=f"http://127.0.0.1:{server.server_port}",
                EXECUTOR_URL=f"http://127.0.0.1:{server.server_port}",
                KRR_RUNNER_KEY=str(key),
                EXECUTOR_DISPATCH_KEY=str(key),
            )
            for _ in range(4):
                if not worker.run_once():
                    break
            else:
                raise AssertionError("Unexpected extra work")
        with sessions()() as db:
            execution = db.scalar(select(Execution))
            outcome = {
                "state": execution.state,
                "holds_lock": execution.holds_lock,
                "maintenance_released": execution.maintenance_released,
            }
        return {
            "jobs": snapshot(),
            "calls": calls,
            "execution": outcome,
            "real_writes": 0,
            "retired_gateway_epoch": retired,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="cloudops-e04-restore-57")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/e04-restore.json")
    parser.add_argument("--inside", choices=["seed", "tick"])
    parser.add_argument("--retired", action="store_true")
    args = parser.parse_args()
    if args.inside:
        assert os.environ["DATABASE_URL"].endswith("/" + DATABASE)
        print(json.dumps(inside(args.inside, args.retired)))
        return
    if not args.project.startswith("cloudops-e04-restore-") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in args.project
    ):
        parser.error("Use a fresh cloudops-e04-restore-* project.")
    if not os.environ.get("POSTGRES_PASSWORD"):
        parser.error("Set a disposable POSTGRES_PASSWORD.")
    lab = ["docker", "compose", "-p", args.project, "-f", "compose.yaml"]

    def command(*argv):
        result = subprocess.run(argv, cwd=ROOT, capture_output=True)
        if result.returncode:
            message = result.stderr.decode(errors="replace")[-1600:]
            raise RuntimeError(message.replace(os.environ["POSTGRES_PASSWORD"], "[redacted]"))
        return result.stdout

    existing = command(
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={args.project}"
    )
    volumes = command(
        "docker",
        "volume",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={args.project}",
    )
    if existing.strip() or volumes.strip():
        parser.error("Project already exists; preserve it and choose a new owned project.")
    command(*lab, "up", "-d", "--wait", "db")
    command(*lab, "exec", "-T", "db", "createdb", "-U", "cloudops", DATABASE)
    image_id = (
        command("docker", "image", "inspect", "cloudops-local-backend", "--format", "{{.Id}}")
        .decode()
        .strip()
    )

    def admin(operation, *extra):
        return json.loads(
            command(
                *lab,
                "run",
                "--rm",
                "-T",
                "dispatch-admin",
                operation,
                *extra,
                "--reason",
                "Owned E04 restore drill",
                "--evidence",
                "probe-e04-restore",
            )
        )

    def run(stage, retired=False):
        return json.loads(
            command(
                "docker",
                "run",
                "--rm",
                "--network",
                args.project + "_default",
                "-e",
                f"DATABASE_URL=postgresql+psycopg://cloudops:{os.environ['POSTGRES_PASSWORD']}@db:5432/{DATABASE}",
                "-e",
                "COOKIE_SECURE=false",
                "-e",
                "WEB_ORIGIN=http://localhost:5173",
                "-e",
                "COLLECTOR_DELAY_SECONDS=0",
                "-e",
                "COLLECTION_SOURCE_MODE=development_fixture",
                "-e",
                "JOB_DISPATCH_ROOT=/run/cloudops-job-permit",
                "-v",
                f"{args.project}_job_dispatch:/run/cloudops-job-permit:ro",
                "-v",
                str(ROOT / "scripts") + ":/review:ro",
                image_id,
                "python",
                "/review/probe-e04-restore.py",
                "--inside",
                stage,
                *(["--retired"] if retired else []),
            )
        )

    report = {
        "schema": "E04ActualRestore/v1",
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "project": args.project,
        "database": DATABASE,
        "worker_image": image_id,
        "proof_class": "Actual pg_dump/pg_restore, host permit CLI, real current handlers and HTTP transports; DB-seeded current records and controlled loopback KRR/gateway fixtures. No Kubernetes writer, actual KRR or E07/E10 executor restore qualification.",
        "steps": {},
    }
    steps = report["steps"]
    steps["bootstrap"] = admin("bootstrap", "--fresh-install")
    steps["snapshot"] = run("seed")
    command(
        *lab,
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "cloudops",
        "-Fc",
        "-f",
        "/tmp/e04.dump",
        DATABASE,
    )
    dump = command(*lab, "exec", "-T", "db", "sha256sum", "/tmp/e04.dump").decode().split()[0]
    report["dump_sha256"] = dump
    steps["consumed_before_restore"] = run("tick")
    assert set(steps["consumed_before_restore"]["calls"]) == {
        "collection",
        "recommendation",
        "recovery_execution",
    }
    steps["enter_recovery"] = admin("enter-recovery")
    command(
        *lab,
        "exec",
        "-T",
        "db",
        "pg_restore",
        "-U",
        "cloudops",
        "--clean",
        "--if-exists",
        "--no-owner",
        "-d",
        DATABASE,
        "/tmp/e04.dump",
    )
    steps["restored_without_permit"] = run("tick", True)
    assert steps["restored_without_permit"]["jobs"] == steps["snapshot"]["jobs"]
    assert steps["restored_without_permit"]["calls"] == []
    resumed = set()
    for job_class, kind in [
        ("collection", "development_collection_v1"),
        ("recommendation", "recommendation_report_v1"),
        ("recovery_execution", "recovery_execution_v1"),
    ]:
        steps["resume_" + job_class] = admin("resume", "--class", job_class)
        result = run("tick", True)
        assert result["calls"] == [job_class]
        assert result["jobs"][kind]["state"] == "completed"
        resumed.add(kind)
        for other in steps["snapshot"]["jobs"]:
            row = result["jobs"][other]
            assert row["id"] == steps["snapshot"]["jobs"][other]["id"]
            assert row["subject_id"] == steps["snapshot"]["jobs"][other]["subject_id"]
            assert row["state"] == ("completed" if other in resumed else "queued")
            assert row["attempts"] == (1 if other in resumed else 0)
        followups = set(result["jobs"]) - set(steps["snapshot"]["jobs"])
        assert followups <= {"recovery_reconciliation_v1"}
        if followups:
            assert job_class == "recovery_execution"
            followup = result["jobs"]["recovery_reconciliation_v1"]
            assert followup["state"] == "queued" and followup["attempts"] == 1
        steps["after_" + job_class] = result
    final = steps["after_recovery_execution"]["execution"]
    assert final == {"state": "reconciling", "holds_lock": True, "maintenance_released": False}
    steps["exit_recovery"] = admin("exit-recovery")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"passed": True, "receipt": str(args.output), "dump_sha256": dump}))


if __name__ == "__main__":
    main()
