"""Linux admission/journal regression tests with a controlled Kubernetes provider.

The separate live probes establish real SO_PEERCRED and Kubernetes fence behavior.
"""

import copy
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_permissions import permission_context as _permission_context

from cloudops.features.recovery.gateway import Dispatch, Gateway
from cloudops.features.recovery.authority import initialize
from cloudops.features.workloads.kubernetes import snapshot
from cloudops.shared.errors import DomainError

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Executor authority uses Linux flock/SO_PEERCRED; exercised in the Linux lab and CI.",
)
permission_context = _permission_context


@pytest.fixture
def executor(permission_context, tmp_path):
    context = permission_context

    class Provider:
        def __init__(self):
            self.value = copy.deepcopy(context.consumer.deployment)
            self.writes = 0
            self.lose_ack = False

        def deployment(self, workload):
            assert workload.uid == self.value["metadata"]["uid"]
            return copy.deepcopy(self.value)

        def controllers(self, workload):
            pass

        def capacity(self, deployment, replicas):
            pass

        def path(self, namespace, name):
            return "controlled-provider"

        def request(self, path, *, patch):
            assert patch[0]["value"] == self.value["metadata"]["uid"]
            if patch[1]["value"] != self.value["metadata"]["resourceVersion"]:
                raise DomainError(
                    409, "target_changed", "Controlled atomic resourceVersion mismatch"
                )
            operation = patch[2]
            if operation["path"] == "/spec/replicas":
                self.writes += 1
                self.value["spec"]["replicas"] = operation["value"]
                for key in ("replicas", "availableReplicas", "readyReplicas", "updatedReplicas"):
                    self.value["status"][key] = operation["value"]
            else:
                self.value["metadata"]["annotations"] = operation["value"]
            self.value["metadata"]["resourceVersion"] = str(uuid4())
            if self.lose_ack and operation["path"] == "/spec/replicas":
                raise DomainError(
                    503,
                    "connection_unavailable",
                    "Controlled acknowledgement loss after application",
                )
            return copy.deepcopy(self.value)

    provider = Provider()
    initialize(tmp_path)
    gate = Gateway(
        tmp_path,
        kube=provider,
        allowlist=[
            {
                "cluster": "permission-provider-fixture",
                "namespace": "demo",
                "name": "consumer",
                "uid": context.consumer.uid,
            }
        ],
    )
    return gate, provider, context.consumer.uid


def maintain(gate, uid, operation, **changes):
    request = {
        "request_id": str(uuid4()),
        "operation": operation,
        "targets": [uid],
        "reason": "Controlled protocol regression",
        "evidence": ["test://controlled-provider"],
        **changes,
    }
    return gate.maintain(request, uid=0, pid=os.getpid()), request


def ready(gate, uid):
    assert maintain(gate, uid, "enable_target")[0]["status"] == "applied"
    if gate.target(uid)["recovery_mode"]:
        assert maintain(gate, uid, "exit_recovery_mode")[0]["status"] == "applied"


def request(gate, provider, uid):
    return Dispatch(
        execution_id=uuid4(),
        uid=uid,
        epoch=gate.target(uid)["epoch"],
        resource_version=provider.value["metadata"]["resourceVersion"],
        replicas=2,
        before=snapshot(provider.value),
        plan_digest="a" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def test_startup_and_restart_retire_authority_independently_of_postgres(executor):
    gate, provider, uid = executor
    assert gate.target(uid)["recovery_mode"] and not gate.target(uid)["enabled"]
    ready(gate, uid)
    old = request(gate, provider, uid)
    restarted = Gateway(gate.root, kube=provider, allowlist=[vars(gate.workloads[uid])])
    assert restarted.target(uid)["recovery_mode"] and not restarted.target(uid)["enabled"]
    assert restarted.target(uid)["epoch"] != old.epoch
    with pytest.raises(DomainError) as error:
        restarted.dispatch(old)
    assert error.value.code == "target_quarantined" and provider.writes == 0


def test_exit_does_not_reconcile_an_already_open_admission_epoch(executor):
    gate, provider, uid = executor
    ready(gate, uid)
    before = gate.target(uid), copy.deepcopy(provider.value)
    result, _ = maintain(gate, uid, "exit_recovery_mode")
    assert result["status"] == "denied" and result["code"] == "maintenance_state_changed"
    assert (gate.target(uid), provider.value) == before


def test_dispatch_is_durable_and_idempotent_for_exact_content(executor):
    gate, provider, uid = executor
    ready(gate, uid)
    body = request(gate, provider, uid)
    first = gate.dispatch(body)
    assert first["outcome"] == "acknowledged"
    assert gate.dispatch(body) == first and provider.writes == 1
    with pytest.raises(DomainError) as error:
        gate.dispatch(body.model_copy(update={"replicas": 3}))
    assert error.value.code == "dispatch_conflict" and provider.writes == 1


def test_proof_releases_safety_without_rewriting_unknown_attribution(executor):
    gate, provider, uid = executor
    ready(gate, uid)
    provider.lose_ack = True
    body = request(gate, provider, uid)
    unknown = gate.dispatch(body)
    assert unknown["outcome"] == "outcome_unknown" and not gate.target(uid)["enabled"]
    result, _ = maintain(gate, uid, "release_uncertain_target")
    assert (
        result["status"] == "applied"
        and str(body.execution_id) in result["proofs"][uid]["excluded_attempts"]
    )
    assert not gate.target(uid)["enabled"]
    ready(gate, uid)
    assert gate.dispatch(body) == unknown and provider.writes == 1
    with gate.connection() as db:
        assert (
            db.execute(
                "SELECT state FROM attempts WHERE id=?", (str(body.execution_id),)
            ).fetchone()[0]
            == "outcome_unknown"
        )
        assert (
            db.execute(
                "SELECT request_id FROM exclusions WHERE attempt_id=?", (str(body.execution_id),)
            ).fetchone()[0]
            == result["request_id"]
        )


@pytest.mark.parametrize(
    "operation", ["enable_target", "release_uncertain_target", "exit_recovery_mode"]
)
def test_no_provider_never_unlocks_or_exits_recovery(executor, operation):
    gate, provider, uid = executor
    gate.provider = "unavailable"
    result, _ = maintain(gate, uid, operation)
    assert result["status"] == "denied" and result["code"] == "maintenance_proof_required"
    assert (
        not gate.target(uid)["enabled"]
        and gate.target(uid)["recovery_mode"]
        and provider.writes == 0
    )


def test_untrusted_actor_and_incomplete_or_forged_fields_cannot_change_authority(executor):
    gate, provider, uid = executor
    before = gate.target(uid)
    raw = {
        "request_id": str(uuid4()),
        "operation": "enable_target",
        "targets": [uid],
        "reason": "Controlled actor denial",
        "evidence": ["test://controlled-provider"],
    }
    assert gate.maintain(raw, uid=10001, pid=1)["code"] == "maintenance_actor_denied"
    for changes in ({"reason": " "}, {"evidence": []}, {"targets": ["other"]}, {"actor": "root"}):
        result = gate.maintain({**raw, "request_id": str(uuid4()), **changes}, uid=0, pid=1)
        assert result["code"] == "maintenance_request_invalid"
    assert gate.target(uid) == before and provider.writes == 0


def test_retrieved_maintenance_receipt_does_not_repeat_enable(executor):
    gate, provider, uid = executor
    applied, raw = maintain(gate, uid, "enable_target")
    assert applied["status"] == "applied"
    maintain(gate, uid, "enter_recovery")
    replayed = gate.maintain(raw, uid=0, pid=1)
    assert replayed["replayed"] and not replayed["current_targets"][0]["enabled"]
    conflict = gate.maintain(
        {**raw, "reason": "Conflicting content under the same identity"}, uid=0, pid=1
    )
    assert conflict["status"] == "denied" and conflict["request_id"] != raw["request_id"]
    with gate.connection() as db:
        assert (
            db.execute("SELECT id FROM audit WHERE id=?", (conflict["request_id"],)).fetchone()
            is not None
        )
