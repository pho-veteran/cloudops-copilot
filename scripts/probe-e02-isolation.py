"""Hold three real TLS observation reads and measure actual HTTPS/product-worker isolation."""

import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from e02_probe_support import arguments, client, lab, login, metadata, password, save


def main():
    args = arguments(__doc__, "isolation.json")
    secret = password(args)
    child = Path(__file__).with_name("e02_observer_stall.py")
    report = metadata(Path(__file__), child)
    try:
        lab.command([*lab.compose, "stop", "observer"])
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            count = lab.app(
                "from sqlalchemy import select,func; from cloudops.shared.database import sessions,database_now; from cloudops.features.workloads.observation_models import ObservationState;\nwith sessions()() as db: print(db.scalar(select(func.count()).select_from(ObservationState).where(ObservationState.lease_until>database_now(db))))"
            )
            due = lab.app(
                "from sqlalchemy import select,func; from cloudops.shared.database import sessions,database_now; from cloudops.features.workloads.observation_models import ObservationState; from cloudops.features.workloads.models import Workload;\nwith sessions()() as db: print(db.scalar(select(func.count()).select_from(ObservationState).join(Workload,Workload.id==ObservationState.workload_id).where(Workload.active,Workload.cluster=='e02-local-k3s',Workload.name.in_(['checkout','e02-ready','e02-recreate']),ObservationState.next_due_at<=database_now(db))))"
            )
            if int(count) == 0 and int(due) == 3:
                break
            time.sleep(1)
        lab.require(
            int(count) == 0 and int(due) == 3,
            "Wait for old leases and natural due times before the isolation probe.",
        )
        process = subprocess.Popen(
            [*lab.compose, "exec", "-T", "api", "python", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ,
            cwd=lab.root,
        )
        try:
            process.stdin.write(child.read_text(encoding="utf-8"))
            process.stdin.close()
            first = process.stdout.readline()
            if not first:
                # Keep exception details private: database/provider errors can include connection information.
                (lab.folder / "isolation-child-error.txt").write_text(
                    process.stderr.read(), encoding="utf-8"
                )
                raise RuntimeError(
                    "Controlled TLS child failed; inspect the private local artifact."
                )
            ready = json.loads(first)
            assert ready["scheduled_leases"] == 3
            with client(args) as connection:
                headers = login(connection, args, secret)
                items = connection.get("/api/workloads").json()["items"]
                target = next(r for r in items if r["name"] == "e02-ready")
                direct = next(r for r in items if r["name"] == "e02-config")
                response = connection.get("/api/workloads/" + direct["id"] + "/baseline-candidate")
                assert response.status_code == 200, response.text
                admitted = time.monotonic()
                response = connection.post(
                    "/api/incidents",
                    json={
                        "workload_id": target["id"],
                        "title": "E02 saturated-reader isolation",
                        "description": "Synthetic investigation evidence; admission while three controlled TLS observation reads are blocked.",
                    },
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                )
                assert response.status_code == 201, response.text
                admission_seconds = time.monotonic() - admitted
                ids = response.json()
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    result = connection.get(
                        f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}"
                    ).json()
                    if result["state"] in {"insufficient_evidence", "failed"}:
                        break
                    time.sleep(0.2)
                assert result["state"] == "insufficient_evidence", result["state"]
                completed_seconds = time.monotonic() - admitted
                live = lab.app(
                    "from sqlalchemy import select,func; from cloudops.shared.database import sessions,database_now; from cloudops.features.workloads.observation_models import ObservationState;\nwith sessions()() as db: print(db.scalar(select(func.count()).select_from(ObservationState).where(ObservationState.purpose=='scheduled',ObservationState.lease_until>database_now(db))))"
                )
                assert int(live) == 3
            tail = process.stdout.read()
            code = process.wait(timeout=20)
            if code != 0:
                (lab.folder / "isolation-child-error.txt").write_text(
                    process.stderr.read(), encoding="utf-8"
                )
                raise RuntimeError(
                    "Controlled TLS child failed; inspect the private local artifact."
                )
            outcome = json.loads(tail.strip())
            assert outcome["outcomes"] == ["attempt_timeout"] * 3 and outcome["closed"] == 3
            report.update(
                observation_transport="Controlled real TLS drip-feed in a separate process; actual scheduled leases and default 12s deadline; not Kubernetes health evidence.",
                product_transport="Actual verified HTTPS API/PostgreSQL/product worker; investigation output remains explicitly synthetic.",
                scheduled_leases_at_product_completion=3,
                fourth_scheduled_denied=ready["fourth_scheduled_denied"],
                reserved_direct_k3s_candidate_succeeded=True,
                admission_seconds=admission_seconds,
                product_job_completed_seconds=completed_seconds,
                reader_outcomes=outcome["outcomes"],
                stalled_connections_closed=outcome["closed"],
                incident_id=ids["incident_id"],
                run_id=ids["run_id"],
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    finally:
        lab.command([*lab.compose, "start", "observer"])
    save(report, args.output)


if __name__ == "__main__":
    main()
