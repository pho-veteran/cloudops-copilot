"""Linux executor-container fault probe: delay an actual conditional patch past a real fence.

Only the functional write transport is delayed. Kubernetes, the durable gateway journal,
PostgreSQL and the SO_PEERCRED maintenance server remain real. No production fault switch.
Stop the application worker before preparing/approving the labeled probe plan.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from cloudops.features.recovery.gateway import Dispatch, Gateway
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.service import ExecutionHandler
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError


def main():
    path = Path("/tmp/e01-late-request.json")
    if path.exists():
        path.unlink()
    release = threading.Event()
    actual = Kubernetes(os.environ["EXECUTOR_KUBE_CONFIG"])
    delayed = {}

    class DelayedTransport:
        def __getattr__(self, key):
            return getattr(actual, key)

        def request(self, path, *, patch=None, **kwargs):
            if patch and any(op["path"] == "/spec/replicas" for op in patch):

                def send_later():
                    if not release.wait(90):
                        delayed["result"] = "probe_timeout"
                        return
                    try:
                        actual.request(path, patch=patch)
                        delayed["result"] = "unexpected_write"
                    except DomainError as error:
                        delayed["result"] = error.code

                delayed["thread"] = threading.Thread(target=send_later, daemon=True)
                delayed["thread"].start()
                raise DomainError(
                    503,
                    "connection_unavailable",
                    "Controlled network delivery/acknowledgement interruption.",
                )
            return actual.request(path, patch=patch, **kwargs)

    gate = Gateway(kube=DelayedTransport())
    print(
        "READY: authority independently quarantined. Enable/exit through the real root CLI, then approve a fresh plan with the worker stopped and provide execution_id in /tmp/e01-late-request.json.",
        flush=True,
    )
    deadline = time.monotonic() + 240
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert path.exists(), "No approved probe identity supplied"
    identity = UUID(json.loads(path.read_text())["execution_id"])
    with sessions()() as db:
        execution = db.get(Execution, identity)
        assert execution.state == "queued" and execution.holds_lock
        plan = db.get(ActionPlan, execution.plan_id)
        admitted_at = execution.created_at.isoformat()
        assert plan.state == "approved"
        request = Dispatch(
            execution_id=identity,
            uid=plan.content["workload"]["uid"],
            epoch=plan.target_epoch,
            resource_version=plan.content["resource_version"],
            replicas=plan.content["replicas"],
            before=plan.content["before"],
            plan_digest=plan.digest,
            expires_at=plan.expires_at,
        )
    result = gate.dispatch(request)
    assert result["outcome"] == "outcome_unknown" and not gate.target(request.uid)["enabled"]

    def maintain(operation):
        command = [
            sys.executable,
            "-m",
            "cloudops.features.recovery.maintenance",
            operation,
            "--target",
            request.uid,
            "--reason",
            "Controlled late-writer E01 acceptance",
            "--evidence",
            f"local://e01/execution/{identity}",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        value = json.loads(result.stdout)
        assert value["status"] == "applied" and value["actor"]["uid"] == 0
        return value

    receipt = maintain("release_uncertain_target")
    proof = receipt["proofs"][request.uid]
    assert (
        str(identity) in proof["excluded_attempts"]
        and proof["resource_version"] != request.resource_version
    )
    assert not gate.target(request.uid)["enabled"]
    release.set()
    delayed["thread"].join(12)
    assert delayed.get("result") == "target_changed", delayed.get("result")
    current = actual.deployment(gate.workloads[request.uid])
    assert current["spec"]["replicas"] == request.before["replicas"]
    with sessions()() as db:
        row = db.get(Execution, identity)
        assert row.state == "outcome_unknown" and row.maintenance_released and not row.holds_lock
        saved = dict(row.result)
    with sessions()() as db, db.begin():
        handler = ExecutionHandler()
        handler.started(db, identity)
        handler.publish(db, identity, {"outcome": "acknowledged", "verification": "inconclusive"})
        handler.failed(db, identity, "late result", terminal=True)
    with sessions()() as db:
        row = db.get(Execution, identity)
        assert row.state == "outcome_unknown" and not row.holds_lock and row.result == saved
    maintain("enable_target")
    try:
        gate.dispatch(request.model_copy(update={"execution_id": uuid4()}))
        raise AssertionError("Retired epoch admitted")
    except DomainError as error:
        assert error.code == "target_quarantined"
    output = {
        "execution_id": str(identity),
        "execution_created_at": admitted_at,
        "uid": request.uid,
        "dispatch": result,
        "maintenance": receipt,
        "late_patch": delayed["result"],
        "replicas_unchanged": current["spec"]["replicas"],
        "retired_epoch_denied": True,
        "late_worker_preserved_unknown": True,
    }
    Path("/tmp/e01-late-proof.json").write_text(json.dumps(output, indent=2))
    print(
        "PASS: real Kubernetes rejected the late old patch after the acknowledged fence; the epoch retired, the target required explicit enable, and delayed worker results preserved historical unknown attribution.",
        flush=True,
    )


if __name__ == "__main__":
    main()
