"""Approved v2 protocol fixtures, not product activation or live model advice."""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_executor import executor as _executor
from test_permissions import permission_context as _permission_context
from consumer_cases import consumer_observation

from cloudops.features.investigations.locators import digest
from cloudops.features.recovery.dispatch import RecoveryDispatch
from cloudops.features.recovery.fields import selected
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.recovery.policy import load
from cloudops.features.workloads import baseline_service
from cloudops.features.workloads.baseline_schemas import BaselinePayload, Provenance
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.models import Baseline, BaselineCandidate, Workload
from cloudops.features.workloads.observation_service import public
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError

executor, permission_context = _executor, _permission_context
CONTROLLERS = Kubernetes.controllers
CAPACITY = Kubernetes.capacity
CAPACITY_BUDGET = Kubernetes.capacity_budget


class Provider:
    def __init__(self, original):
        self.value = original.value
        self.value["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
            "cpu": "100m",
            "memory": "128Mi",
        }
        self.writes, self.lose_ack = 0, False
        self.patches, self.hpas, self.vpas = [], [], []
        self.unavailable = None
        self.nodes = [
            {
                "metadata": {"name": "fixture-node"},
                "spec": {},
                "status": {
                    "allocatable": {"cpu": "4", "memory": "4Gi", "pods": "100"},
                    "conditions": [
                        {"type": name, "status": "True" if name == "Ready" else "False"}
                        for name in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure")
                    ],
                },
            }
        ]
        self.pods = [
            {
                "metadata": {"uid": "fixture-pod"},
                "spec": {
                    "nodeName": "fixture-node",
                    **copy.deepcopy(self.value["spec"]["template"]["spec"]),
                },
                "status": {"phase": "Running"},
            }
        ]

    def deployment(self, workload):
        return copy.deepcopy(self.value)

    def controllers(self, workload):
        return CONTROLLERS(self, workload)

    def capacity(self, deployment, replicas):
        return CAPACITY(self, deployment, replicas)

    def capacity_budget(self, deployment, extra):
        return CAPACITY_BUDGET(self, deployment, extra)

    def path(self, namespace, name):
        return "controlled-provider"

    def request(self, path, *, patch=None, optional=False):
        if path == self.unavailable:
            raise DomainError(503, "cluster_unavailable", "Controlled source unavailable.")
        if patch is None:
            if path == "/api/v1/nodes":
                return {"items": copy.deepcopy(self.nodes)}
            if path == "/api/v1/pods":
                return {"items": copy.deepcopy(self.pods)}
            if path.endswith("/horizontalpodautoscalers"):
                return {"items": copy.deepcopy(self.hpas)}
            if path.endswith("/verticalpodautoscalers"):
                return {"items": copy.deepcopy(self.vpas)}
            raise AssertionError(path)
        assert patch[0] == {
            "op": "test",
            "path": "/metadata/uid",
            "value": self.value["metadata"]["uid"],
        }
        if patch[1]["value"] != self.value["metadata"]["resourceVersion"]:
            raise DomainError(409, "target_changed", "Controlled atomic stale precondition.")
        self.patches.append(copy.deepcopy(patch))
        functional = any(op["path"].startswith("/spec/") for op in patch[2:])
        for operation in patch[2:]:
            parts = operation["path"].strip("/").split("/")
            target = self.value
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            key = int(parts[-1]) if isinstance(target, list) else parts[-1]
            if operation["op"] == "replace":
                assert key in target if isinstance(target, dict) else key < len(target)
            target[key] = copy.deepcopy(operation["value"])
        self.value["metadata"]["resourceVersion"] = str(uuid4())
        if functional:
            self.writes += 1
            if self.lose_ack:
                raise DomainError(503, "cluster_unavailable", "Controlled acknowledgement loss.")
        return copy.deepcopy(self.value)


@pytest.fixture
def gateway_case(executor, permission_context, tmp_path, monkeypatch):
    gate, original, uid = executor
    provider = Provider(original)
    gate.kube = provider
    containers = selected(provider.value)[0]
    rules = {
        "schema_version": "RecoveryPolicy/v1",
        "id": "gateway-fixture",
        "version": "1",
        "workload_uids": [uid],
        "max_cpu": "1",
        "max_memory": "512Mi",
        "qualified_images": ["example/fixture:1", "example/fixture:2"],
        "memory_profiles": [
            {
                "id": "fixture-memory",
                "version": "1",
                "workload_uid": uid,
                "containers": ["consumer"],
                "request": "64Mi",
                "limit": "256Mi",
                "qualification_reference": "test://controlled-not-live",
            }
        ],
        "verification_policy": "fixture-unqualified/v1",
        "inverse_policy": "fixture-explicit-old-state/v1",
        "inverse_qualified_states": [
            {
                "workload_uid": uid,
                "containers": containers,
                "qualification_reference": "test://controlled-old-state",
            }
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(rules))
    monkeypatch.setenv("RECOVERY_POLICY_FILE", str(path))
    return SimpleNamespace(
        gate=gate,
        provider=provider,
        uid=uid,
        context=permission_context,
        rules=rules,
        policy_path=path,
    )


def current(case):
    return {
        "uid": case.uid,
        "replicas": case.provider.value["spec"].get("replicas", 1),
        "containers": selected(case.provider.value)[0],
    }


def baseline(case):
    with sessions()() as db, db.begin():
        workload = db.get(Workload, UUID(case.context.consumer.workload_id))
        now = datetime.now(timezone.utc)
        first = consumer_observation(workload, case.provider.value, now - timedelta(seconds=10))
        second = consumer_observation(workload, case.provider.value, now)
        saved = BaselinePayload(
            **baseline_service.envelope(second).model_dump(),
            provenance=Provenance(first=public(first), second=public(second)),
        )
        snapshot = saved.model_dump(mode="json", exclude_none=True)
        hashed = baseline_service.digest(saved)
        candidate = BaselineCandidate(workload_id=workload.id, digest=hashed, snapshot=snapshot)
        db.add(candidate)
        db.flush()
        row = Baseline(
            workload_id=workload.id,
            candidate_id=candidate.id,
            digest=hashed,
            snapshot=snapshot,
            confirmed_by=UUID(case.context.actors["operator"].session["principal"]["id"]),
        )
        db.add(row)
        db.flush()
        return {
            "baseline_id": str(row.id),
            "baseline_digest": row.digest,
            "baseline_profile": saved.profile,
        }


def intent(case, template="set_memory_profile", *, direction="forward", forward=None):
    before = current(case)
    desired = copy.deepcopy(before)
    if direction == "inverse":
        desired = forward.before.selected()
        selector = {
            "forward_operation_id": str(forward.operation_id),
            "forward_plan_id": str(forward.plan_id),
        }
    elif template == "scale_deployment":
        desired["replicas"] = 2
        selector = {"replicas": 2}
    elif template == "restore_deployment_template":
        selector = baseline(case)
        case.provider.value["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "example/fixture:2"
        )
        before = current(case)
        case.rules["inverse_qualified_states"].append(
            {
                "workload_uid": case.uid,
                "containers": before["containers"],
                "qualification_reference": "test://controlled-image-inverse",
            }
        )
        case.policy_path.write_text(json.dumps(case.rules))
    else:
        desired["containers"][0]["resources"]["requests"]["memory"] = "64Mi"
        desired["containers"][0]["resources"]["limits"]["memory"] = "256Mi"
        profile = case.rules["memory_profiles"][0]
        selector = {
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "profile_digest": digest(profile),
        }
    policy = load()
    operation_id = uuid4()
    return RecoveryDispatch(
        schema_version="RecoveryDispatch/v2",
        operation_id=operation_id,
        execution_id=operation_id,
        direction=direction,
        template=template,
        plan_id=uuid4(),
        plan_version=1,
        plan_digest="0" * 64,
        uid=case.uid,
        epoch=case.gate.target(case.uid)["epoch"],
        resource_version=case.provider.value["metadata"]["resourceVersion"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        policy={"id": policy.id, "version": policy.version, "digest": policy.digest()},
        before=before,
        desired=desired,
        selector=selector,
    )


def approve(case, body):
    content = {
        "schema_version": "RecoveryPlan/v2",
        "source": "controlled_gateway_protocol_fixture",
        "direction": body.direction,
        "execution_capability": "conditional_dispatch_v2",
        "expires_at": body.expires_at.isoformat(),
        "workload": {
            key: getattr(case.gate.workloads[case.uid], key)
            for key in ("cluster", "namespace", "name", "uid")
        },
        "before": body.before.selected(),
        "desired": body.desired.selected(),
        "selector": body.selector.model_dump(mode="json"),
        "policy": body.policy.model_dump(mode="json"),
    }
    body = body.model_copy(update={"plan_digest": digest(content)})
    with sessions()() as db, db.begin():
        for row in db.query(Execution).filter(
            Execution.workload_id == UUID(case.context.consumer.workload_id),
            Execution.holds_lock.is_(True),
        ):
            row.state, row.holds_lock = "manual_intervention", False
        db.flush()
        row = ActionPlan(
            id=body.plan_id,
            incident_id=UUID(case.context.consumer.incident_id),
            workload_id=UUID(case.context.consumer.workload_id),
            template=body.template,
            content=content,
            digest=body.plan_digest,
            target_epoch=body.epoch,
            state="approved",
            decided_by=UUID(case.context.actors["operator"].session["principal"]["id"]),
            decided_at=datetime.now(timezone.utc),
            rationale="Controlled gateway fixture, not product activation",
            created_by=UUID(case.context.actors["operator"].session["principal"]["id"]),
            expires_at=body.expires_at,
        )
        db.add(row)
        db.flush()
        db.add(
            Execution(
                id=body.execution_id,
                plan_id=row.id,
                workload_id=row.workload_id,
                state="executing",
                holds_lock=True,
            )
        )
    return body
