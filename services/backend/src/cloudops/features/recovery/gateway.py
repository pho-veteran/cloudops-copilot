"""Single-host executor and OS-authenticated maintenance channel; no web maintenance route."""

import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, Header
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from cloudops.features.recovery.models import Execution
from cloudops.features.recovery.authority import identity_path, read_identity
from cloudops.features.recovery.capacity import candidate_capacity
from cloudops.features.recovery.dispatch import RecoveryDispatch, RecoveryReceipt, conditional_patch
from cloudops.features.recovery.dispatch_validation import validate_intent
from cloudops.features.workloads.kubernetes import Kubernetes, healthy, snapshot
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_evaluator import ObservationError
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError, domain_error_handler

FENCE = "cloudops-copilot.local/writer-fence"


def receipt(body, result):
    if not isinstance(body, RecoveryDispatch):
        return result
    acknowledged = result["outcome"] == "acknowledged"
    return RecoveryReceipt(
        operation_id=body.operation_id,
        plan_id=body.plan_id,
        plan_digest=body.plan_digest,
        attribution="not_sent" if result.get("code") == "no_op" else result["outcome"],
        code=result.get("code", result["outcome"]),
        reason=result["reason"],
        acknowledged_resource_version=result.get("resource_version") if acknowledged else None,
        acknowledged_at=datetime.now(timezone.utc) if acknowledged else None,
    ).model_dump(mode="json")


class Dispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: UUID
    uid: str = Field(min_length=1, max_length=100)
    epoch: str = Field(min_length=1, max_length=64)
    resource_version: str = Field(min_length=1, max_length=128)
    replicas: int = Field(strict=True, ge=1, le=3)
    before: dict
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


class Maintenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    operation: Literal[
        "status",
        "enter_recovery",
        "enable_target",
        "release_uncertain_target",
        "exit_recovery_mode",
    ]
    targets: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class Gateway:
    def __init__(self, root=None, *, kube=None, allowlist=None, identity_file=None):
        self.root = Path(root or os.environ.get("EXECUTOR_ROOT", "/var/lib/cloudops-executor"))
        self.identity_file = identity_path(self.root, identity_file)
        self.identity = read_identity(self.root, self.identity_file)
        self.path = self.root / "authority.sqlite"
        self.kube = kube or Kubernetes(os.environ.get("EXECUTOR_KUBE_CONFIG"))
        entries = allowlist or json.loads(Path(os.environ["EXECUTOR_ALLOWLIST"]).read_text())
        self.workloads = {entry["uid"]: SimpleNamespace(**entry) for entry in entries}
        if not self.workloads or len(self.workloads) != len(entries) or len(entries) > 100:
            raise RuntimeError("Configure unique bounded executor target UIDs.")
        self.provider = os.environ.get("EXECUTOR_PROOF_PROVIDER", "resource_version_fence_v1")
        with self.locked(), self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets (uid TEXT PRIMARY KEY, epoch TEXT NOT NULL, enabled INTEGER NOT NULL, proof TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, uid TEXT NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, digest TEXT NOT NULL, actor TEXT NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL, observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exclusions (attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inverse_links (forward_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE);
            """)
            db.execute("INSERT OR REPLACE INTO control VALUES ('schema_version', '2')")
            db.execute("INSERT OR REPLACE INTO control VALUES ('recovery_mode', 'true')")
            db.execute(
                "UPDATE targets SET enabled=0, epoch=?, reason='Executor startup requires trusted reconciliation.'",
                (str(uuid4()),),
            )
            db.commit()
            for interrupted in db.execute(
                "SELECT id,request FROM attempts WHERE state='dispatching'"
            ).fetchall():
                saved = json.loads(interrupted["request"])
                body = (
                    RecoveryDispatch.model_validate(saved)
                    if "schema_version" in saved
                    else Dispatch.model_validate(saved)
                )
                result = receipt(
                    body,
                    {
                        "outcome": "outcome_unknown",
                        "verification": "inconclusive",
                        "reason": "Executor interrupted after durable dispatch admission; old-writer proof is required.",
                    },
                )
                db.execute(
                    "UPDATE attempts SET state='outcome_unknown', result=? WHERE id=?",
                    (json.dumps(result), interrupted["id"]),
                )
            for uid in self.workloads:
                db.execute(
                    "INSERT OR IGNORE INTO targets VALUES (?, ?, 0, '{}', 'Initial target quarantine.')",
                    (uid, str(uuid4())),
                )

    @contextmanager
    def connection(self):
        db = None
        try:
            pinned = read_identity(self.root, self.identity_file)
            db = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=15)
            actual = db.execute("SELECT value FROM control WHERE key='deployment_id'").fetchone()
            if pinned != self.identity or not actual or actual[0] != pinned["deployment_id"]:
                db.close()
                raise RuntimeError("Authority deployment changed.")
        except (RuntimeError, sqlite3.Error):
            if db is not None:
                db.close()
            raise DomainError(
                503,
                "authority_unavailable",
                "Pinned executor authority is unavailable; no admission or replacement initialization is permitted.",
            ) from None
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self):
        import fcntl

        read_identity(self.root, self.identity_file)
        with (self.root / "admission.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def target(self, uid):
        # WAL reads deliberately do not wait on the admission lock. This avoids a
        # Postgres-workload-lock -> gateway-lock -> Postgres maintenance deadlock.
        with self.connection() as db:
            row = db.execute("SELECT * FROM targets WHERE uid=?", (uid,)).fetchone()
            if not row or uid not in self.workloads:
                raise DomainError(
                    404, "target_unavailable", "Target is outside the executor allowlist."
                )
            return {
                "deployment_id": self.identity["deployment_id"],
                "uid": uid,
                "epoch": row["epoch"],
                "enabled": bool(row["enabled"]),
                "recovery_mode": json.loads(
                    db.execute("SELECT value FROM control WHERE key='recovery_mode'").fetchone()[0]
                ),
                "reason": row["reason"],
                "proof": json.loads(row["proof"]),
                "historical_unknown_count": db.execute(
                    "SELECT count(*) FROM attempts WHERE uid=? AND state='outcome_unknown'", (uid,)
                ).fetchone()[0],
            }

    def dispatch(self, body: Dispatch | RecoveryDispatch):
        request = body.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        identity = str(body.execution_id)
        version_two = isinstance(body, RecoveryDispatch)
        with self.locked(), self.connection() as db:
            prior = db.execute("SELECT * FROM attempts WHERE id=?", (identity,)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise DomainError(
                        409,
                        "dispatch_conflict",
                        "Execution identity was already used with different content.",
                    )
                return json.loads(prior["result"])
            authority = self.target(body.uid)
            if (
                not authority["enabled"]
                or authority["recovery_mode"]
                or authority["epoch"] != body.epoch
            ):
                raise DomainError(
                    409,
                    "target_quarantined",
                    "Target admission is closed or the writer epoch is retired.",
                )
            workload = self.workloads[body.uid]
            try:
                if body.expires_at.tzinfo is None or body.expires_at <= datetime.now(timezone.utc):
                    raise DomainError(
                        409, "plan_expired", "The plan expired before gateway admission."
                    )
                current = self.kube.deployment(workload)
                self.kube.controllers(workload)
                if version_two:
                    validate_intent(body, workload, current, db)
                    candidate_capacity(self.kube, current, body)
                    patch = conditional_patch(body, current)
                else:
                    self.kube.capacity(current, body.replicas)
                    if (
                        current["metadata"]["resourceVersion"] != body.resource_version
                        or snapshot(current) != body.before
                    ):
                        raise DomainError(
                            409, "target_changed", "Live fields changed since the approved plan."
                        )
                    patch = [
                        {"op": "test", "path": "/metadata/uid", "value": body.uid},
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": body.resource_version,
                        },
                        {"op": "replace", "path": "/spec/replicas", "value": body.replicas},
                    ]
                if body.expires_at <= datetime.now(timezone.utc):
                    raise DomainError(
                        409, "plan_expired", "The plan expired during gateway eligibility checks."
                    )
            except (
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                ArithmeticError,
                ObservationError,
            ):
                result = receipt(
                    body,
                    {
                        "outcome": "denied",
                        "reason": "Retained intent or selected fields are unavailable or unsupported.",
                        "code": "intent_ineligible",
                    },
                )
                db.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?, 'denied', ?)",
                    (identity, body.uid, json.dumps(request), digest, json.dumps(result)),
                )
                return result
            except DomainError as error:
                result = receipt(
                    body,
                    {
                        "outcome": "denied",
                        "verification": "not_performed",
                        "reason": error.message,
                        "code": error.code,
                    },
                )
                db.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?, 'denied', ?)",
                    (identity, body.uid, json.dumps(request), digest, json.dumps(result)),
                )
                return result
            pending = receipt(
                body,
                {
                    "outcome": "outcome_unknown",
                    "verification": "inconclusive",
                    "reason": "The admitted write has no durable acknowledgement yet.",
                },
            )
            db.execute(
                "UPDATE targets SET enabled=0, reason='Write admitted; awaiting durable acknowledgement.' WHERE uid=?",
                (body.uid,),
            )
            db.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, 'dispatching', ?)",
                (identity, body.uid, json.dumps(request), digest, json.dumps(pending)),
            )
            if version_two and body.direction == "inverse":
                db.execute(
                    "INSERT INTO inverse_links VALUES (?, ?)",
                    (str(body.selector.forward_operation_id), identity),
                )
            db.commit()  # Must reach durable storage before the network write.
            try:
                changed = self.kube.request(
                    self.kube.path(workload.namespace, workload.name), patch=patch
                )
                result = {
                    "outcome": "acknowledged",
                    "verification": "inconclusive",
                    "reason": "Kubernetes acknowledged the conditional selected-field change; application verification is separate."
                    if version_two
                    else "Kubernetes acknowledged the conditional replica change. Application traffic/metric coverage is unavailable; verify manually.",
                    "resource_version": changed["metadata"]["resourceVersion"],
                    "replicas": body.desired.replicas if version_two else body.replicas,
                }
                db.execute(
                    "UPDATE targets SET enabled=1, reason='Conditional write acknowledged; application verification remains manual.' WHERE uid=?",
                    (body.uid,),
                )
            except DomainError as error:
                if error.code == "target_changed":
                    result = {
                        "outcome": "denied",
                        "verification": "not_performed",
                        "reason": "The atomic UID/resourceVersion precondition failed; no change was admitted.",
                    }
                    db.execute(
                        "UPDATE targets SET enabled=1, reason='Conditional write rejected without application.' WHERE uid=?",
                        (body.uid,),
                    )
                else:
                    result = {
                        "outcome": "outcome_unknown",
                        "verification": "inconclusive",
                        "reason": "The admitted write has no durable acknowledgement yet.",
                    }
                    db.execute(
                        "UPDATE targets SET reason='Write acknowledgement unknown; retain quarantine.' WHERE uid=?",
                        (body.uid,),
                    )
            state = result["outcome"]
            result = receipt(body, result)
            db.execute(
                "UPDATE attempts SET state=?, result=? WHERE id=?",
                (state, json.dumps(result), identity),
            )
            return result

    def operation(self, operation_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT result FROM attempts WHERE id=?", (str(operation_id),)
            ).fetchone()
            if row is None:
                raise DomainError(
                    404,
                    "operation_not_found",
                    "No durable operation receipt is available; absence does not prove no write.",
                )
            return json.loads(row["result"])

    def prove(self, uid, db):
        workload = self.workloads[uid]
        current = self.kube.deployment(workload)
        self.kube.controllers(workload)
        if not healthy(current):
            raise DomainError(
                409,
                "maintenance_state_changed",
                "Target is not fully ready. Repair it manually before enabling product writes.",
            )
        unresolved = db.execute(
            "SELECT id, request FROM attempts WHERE uid=? AND state IN ('dispatching', 'outcome_unknown') AND id NOT IN (SELECT attempt_id FROM exclusions)",
            (uid,),
        ).fetchall()
        for item in unresolved:
            captured = json.loads(item["request"])
            try:
                retained = (
                    RecoveryDispatch.model_validate(captured)
                    if "schema_version" in captured
                    else Dispatch.model_validate(captured)
                )
            except ValueError:
                raise DomainError(
                    409,
                    "maintenance_proof_required",
                    "An unresolved writer has an unsupported or incomplete captured protocol.",
                ) from None
            if retained.uid != uid or not retained.resource_version:
                raise DomainError(
                    409,
                    "maintenance_proof_required",
                    "An unresolved writer lacks its captured preconditions.",
                )
        nonce = str(uuid4())
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {
                "op": "add",
                "path": "/metadata/annotations",
                "value": {**current["metadata"].get("annotations", {}), FENCE: nonce},
            },
        ]
        self.kube.request(self.kube.path(workload.namespace, workload.name), patch=patch)
        confirmed = self.kube.deployment(workload)
        deadline = time.monotonic() + 5
        while not healthy(confirmed) and time.monotonic() < deadline:
            time.sleep(0.2)
            confirmed = self.kube.deployment(workload)
        if (
            confirmed["metadata"].get("annotations", {}).get(FENCE) != nonce
            or not healthy(confirmed)
            or any(
                json.loads(item["request"])["resource_version"]
                == confirmed["metadata"]["resourceVersion"]
                for item in unresolved
            )
        ):
            raise DomainError(
                409,
                "maintenance_proof_required",
                "Fresh fence acknowledgement/current-state validation failed.",
            )
        return {
            "provider": "resource_version_fence_v1",
            "uid": uid,
            "nonce": nonce,
            "resource_version": confirmed["metadata"]["resourceVersion"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "excluded_attempts": [r["id"] for r in unresolved],
            "snapshot": snapshot(confirmed),
        }

    def maintain(self, raw, *, uid, pid):
        identity = str(uuid4())
        request = {"operation": "invalid", "reason": "Invalid maintenance request."}
        digest = "invalid"
        actor = {
            "uid": uid,
            "pid": pid,
            "account": "root" if uid == 0 else "untrusted",
            "channel": "local_unix_peer_credentials",
        }
        with self.locked(), self.connection() as db:
            actor["deployment_id"] = db.execute(
                "SELECT value FROM control WHERE key='deployment_id'"
            ).fetchone()[0]
            try:
                if uid != 0:
                    raise DomainError(
                        403,
                        "maintenance_actor_denied",
                        "A trusted deployment-root invocation is required; app credentials grant no maintenance authority.",
                    )
                try:
                    body = Maintenance.model_validate(raw)
                except ValidationError:
                    raise DomainError(
                        422,
                        "maintenance_request_invalid",
                        "Use a supported operation with bounded reason, evidence and targets; actor fields are not accepted.",
                    ) from None
                identity = str(body.request_id)
                request = body.model_dump(mode="json")
                digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
                prior = db.execute(
                    "SELECT digest, result FROM audit WHERE id=?", (identity,)
                ).fetchone()
                if prior:
                    if prior["digest"] != digest:
                        identity = str(uuid4())
                        raise DomainError(
                            409,
                            "maintenance_request_invalid",
                            "Request identity content changed; a separate denial receipt was recorded.",
                        )
                    return {
                        **json.loads(prior["result"]),
                        "replayed": True,
                        "current_targets": [
                            self.target(t) for t in body.targets if t in self.workloads
                        ],
                    }
                if body.operation == "status":
                    return {
                        "targets": [self.target(k) for k in self.workloads],
                        "audit": [
                            dict(r)
                            for r in db.execute(
                                "SELECT id, actor, request, result, observed_at FROM audit ORDER BY rowid DESC LIMIT 25"
                            )
                        ],
                        "unknown_attributions": [
                            dict(r)
                            for r in db.execute(
                                "SELECT a.id, a.uid, a.state, e.request_id AS safety_exclusion_request FROM attempts a LEFT JOIN exclusions e ON e.attempt_id=a.id WHERE a.state='outcome_unknown' ORDER BY a.rowid DESC LIMIT 100"
                            )
                        ],
                    }
                if (
                    not body.reason.strip()
                    or not body.evidence
                    or any(not e.strip() or len(e) > 512 for e in body.evidence)
                    or not body.targets
                    or len(set(body.targets)) != len(body.targets)
                    or not set(body.targets) <= self.workloads.keys()
                ):
                    raise DomainError(
                        422,
                        "maintenance_request_invalid",
                        "Reason, evidence references and configured target identities are required.",
                    )
                if (
                    body.operation in ("enter_recovery", "exit_recovery_mode")
                    and set(body.targets) != self.workloads.keys()
                ):
                    raise DomainError(
                        422,
                        "maintenance_request_invalid",
                        "Recovery mode operations must include every configured target.",
                    )
                if (
                    body.operation != "enter_recovery"
                    and self.provider != "resource_version_fence_v1"
                ):
                    raise DomainError(
                        409,
                        "maintenance_proof_required",
                        "No supported safety proof provider is configured.",
                    )
                if body.operation == "exit_recovery_mode" and not all(
                    self.target(t)["recovery_mode"] for t in body.targets
                ):
                    raise DomainError(
                        409,
                        "maintenance_state_changed",
                        "Recovery mode is already exited. Start a new explicit recovery reconciliation before attempting another exit.",
                    )
                if body.operation == "exit_recovery_mode" and any(
                    not self.target(t)["enabled"] for t in body.targets
                ):
                    raise DomainError(
                        409,
                        "maintenance_proof_required",
                        "Explicitly enable every configured target before exiting recovery mode.",
                    )
                if body.operation != "exit_recovery_mode":
                    for target in body.targets:
                        db.execute(
                            "UPDATE targets SET enabled=0, epoch=?, reason='Trusted maintenance closed admission and retired old authority.' WHERE uid=?",
                            (str(uuid4()), target),
                        )
                    db.commit()
                if body.operation == "enter_recovery":
                    db.execute("INSERT OR REPLACE INTO control VALUES ('recovery_mode', 'true')")
                    result = {
                        "status": "applied",
                        "operation": body.operation,
                        "reason": "All configured targets quarantined independently of application database contents.",
                    }
                else:
                    proofs = {target: self.prove(target, db) for target in body.targets}
                    # Root-held gateway admission stays closed if the application commit fails.
                    with sessions()() as app_db, app_db.begin():
                        for target in body.targets:
                            workload = self.workloads[target]
                            rows = app_db.scalars(
                                select(Workload)
                                .where(
                                    Workload.uid == target,
                                    Workload.cluster == workload.cluster,
                                    Workload.namespace == workload.namespace,
                                    Workload.name == workload.name,
                                )
                                .with_for_update()
                            ).all()
                            for row in rows:
                                executions = app_db.scalars(
                                    select(Execution)
                                    .where(Execution.workload_id == row.id, Execution.holds_lock)
                                    .with_for_update()
                                ).all()
                                for execution in executions:
                                    (
                                        execution.state,
                                        execution.holds_lock,
                                        execution.maintenance_released,
                                    ) = "outcome_unknown", False, True
                                    execution.result = {
                                        **execution.result,
                                        "outcome": "outcome_unknown",
                                        "maintenance_request_id": identity,
                                        "reason": "Trusted maintenance established current safety; historical write attribution remains unknown.",
                                    }
                    if body.operation == "exit_recovery_mode":
                        db.execute(
                            "INSERT OR REPLACE INTO control VALUES ('recovery_mode', 'false')"
                        )
                    for target, proof in proofs.items():
                        for attempt in proof["excluded_attempts"]:
                            db.execute(
                                "INSERT OR IGNORE INTO exclusions VALUES (?, ?)",
                                (attempt, identity),
                            )
                        db.execute(
                            "UPDATE targets SET enabled=?, proof=?, reason='Trusted current-state and old-writer exclusion proof recorded.' WHERE uid=?",
                            (
                                body.operation != "release_uncertain_target",
                                json.dumps(proof),
                                target,
                            ),
                        )
                    result = {
                        "status": "applied",
                        "operation": body.operation,
                        "proofs": proofs,
                        "reason": "Proof/current-state checks passed; historical unknown outcomes were preserved.",
                    }
            except DomainError as error:
                result = {"status": "denied", "code": error.code, "reason": error.message}
            except Exception:
                result = {
                    "status": "outcome_unknown",
                    "code": "maintenance_unavailable",
                    "reason": "Maintenance could not durably complete. Preserve quarantine and inspect the request record; no success is claimed.",
                }
                for target in self.workloads:
                    db.execute("UPDATE targets SET enabled=0 WHERE uid=?", (target,))
            result["request_id"] = identity
            result["actor"] = actor
            db.execute(
                "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    digest,
                    json.dumps(actor),
                    json.dumps(request),
                    json.dumps(result),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return result


def create_gateway_app(gate, dispatch_key):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(DomainError, domain_error_handler)

    @app.get("/targets/{uid}")
    def target(uid: str):
        return gate.target(uid)

    @app.post("/dispatch")
    def dispatch(body: Dispatch | RecoveryDispatch, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + dispatch_key).encode()):
            raise DomainError(403, "dispatch_denied", "Executor dispatch credential required.")
        return gate.dispatch(body)

    @app.get("/operations/{operation_id}")
    def operation(operation_id: UUID, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + dispatch_key).encode()):
            raise DomainError(403, "dispatch_denied", "Executor dispatch credential required.")
        return gate.operation(operation_id)

    return app


def main():
    if os.getuid() != 0:
        raise RuntimeError("Executor authority must run in its isolated deployment-root container.")
    gate = Gateway()
    key_path = Path(os.environ["EXECUTOR_DISPATCH_KEY"])
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key_path.write_text(secrets.token_urlsafe(32))
        key_path.chmod(0o600)
    dispatch_key = key_path.read_text().strip()
    app = create_gateway_app(gate, dispatch_key)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(15)
            pid, uid, _ = struct.unpack(
                "3i",
                self.request.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                ),
            )
            try:
                raw = self.rfile.readline(16385)
                value = json.loads(raw) if len(raw) <= 16384 else {}
            except (ValueError, OSError):
                value = {}
            result = gate.maintain(value, uid=uid, pid=pid)
            self.wfile.write(json.dumps(result).encode() + b"\n")

    socket_path = gate.root / "maintenance.sock"
    if socket_path.exists():
        socket_path.unlink()
    server = socketserver.ThreadingUnixStreamServer(str(socket_path), Handler)
    # Local peer credentials enforce authority; no application container mounts this socket.
    gate.root.chmod(0o755)
    socket_path.chmod(0o666)
    gate.path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8081, access_log=False)


if __name__ == "__main__":
    main()
