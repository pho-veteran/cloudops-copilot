"""Actual PostgreSQL/API/jobs with explicitly controlled, clocked external observations."""

import copy
import json
import threading
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from observation_fixtures import objects, observed
from recovery_verification_fixtures import metrics, profile
from sqlalchemy import select
from alembic import command
from alembic.config import Config
from test_recovery_plans import candidate as _candidate
from test_permissions import permission_context as _permission_context
from test_permissions import snapshot

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.recovery import service, verification, verification_policy
from cloudops.features.investigations.models import InvestigationRun, Evidence
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.incidents.models import IncidentActivity
from cloudops.features.recovery.fields import selected
from cloudops.features.recovery.models import ActionPlan, Execution, Verification
from cloudops.features.recovery.verification_policy import VERIFICATION_JOB
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.observation_schemas import Identity
from cloudops.shared.database import database_now, sessions
from cloudops.shared.database import BACKEND_ROOT
from cloudops.shared.jobs import Job, claim_next, finish

candidate, permission_context = _candidate, _permission_context


@pytest.fixture
def qualified(candidate, monkeypatch):
    c = candidate
    c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
        "cpu": "100m",
        "memory": "64Mi",
    }
    value = profile().model_dump(mode="json")
    value.update(
        workload_uid=c.consumer.uid,
        containers=selected(c.consumer.deployment)[0],
        replicas=[2],
        image_ids={"consumer": "sha256:" + "b" * 64},
    )
    p = verification_policy.VerificationProfile.model_validate(value)
    c.recovery_rules["verification_policy"] = p.id
    c.recovery_path.write_text(json.dumps(c.recovery_rules))

    def lookup(uid, template, desired):
        return p if p.matches(uid, template, desired) else None

    monkeypatch.setattr(verification_policy, "load_for", lookup)
    monkeypatch.setattr("cloudops.features.recovery.plans.load_for", lookup)
    monkeypatch.setattr(Kubernetes, "capacity_budget", lambda *_args: None)
    c.verification_profile = p
    return c


def propose(c):
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/plans",
        headers=actor.headers,
        json={
            "origin": "operator_manual",
            "replicas": 2,
            "rationale": "Controlled verification lifecycle; no live writes.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(c, plan):
    actor = c.actors["operator"]
    path = f"/api/plans/{plan['id']}"
    reviewed = actor.client.get(path).json()
    assert reviewed["valid"], reviewed
    response = actor.client.post(
        path + "/decision",
        headers=actor.headers,
        json={
            "version": plan["version"],
            "digest": plan["digest"],
            "review_token": reviewed["review_token"],
            "decision": "approve",
        },
    )
    assert response.status_code == 200, response.text
    with sessions()() as db:
        return db.scalar(select(Execution.id).where(Execution.plan_id == UUID(plan["id"])))


def controlled_capture(c, workload, at, *, replicas, start, end, include_metrics):
    data = objects()
    deployment = copy.deepcopy(c.consumer.deployment)
    deployment.update(kind="Deployment", apiVersion="apps/v1")
    deployment["metadata"].update(name=workload.name, namespace=workload.namespace)
    deployment["spec"]["replicas"] = replicas
    deployment["status"] = {
        "observedGeneration": 1,
        **{
            key: replicas
            for key in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")
        },
    }
    data[0].clear()
    data[0].update(deployment)
    rs = data[1]["items"][0]
    rs["metadata"]["ownerReferences"][0]["uid"] = workload.uid
    rs["spec"]["template"] = copy.deepcopy(deployment["spec"]["template"])
    first = data[2]["items"][0]
    first["spec"] = copy.deepcopy(deployment["spec"]["template"]["spec"])
    first["status"]["containerStatuses"][0]["name"] = "consumer"
    pods = []
    for index in range(replicas):
        pod = copy.deepcopy(first)
        pod["metadata"].update(uid=f"pod-{index}", name=f"consumer-{index}")
        pods.append(pod)
    data[2]["items"] = pods
    identity = Identity(
        workload_id=workload.id,
        cluster=workload.cluster,
        namespace=workload.namespace,
        name=workload.name,
        uid=workload.uid,
        epoch=workload.observation_epoch,
    )
    result = {
        "rollout": observed(identity, at, data).model_dump(mode="json"),
        "state": {
            "coverage": {"status": "available"},
            "redacted_bounded_payload": {
                "pods": [
                    {
                        "uid": f"pod-{i}",
                        "containers": [
                            {
                                "name": "consumer",
                                "kind": "regular",
                                "image_id": "sha256:" + "b" * 64,
                            }
                        ],
                    }
                    for i in range(replicas)
                ]
            },
        },
        "events": {"coverage": {"status": "available"}, "redacted_bounded_payload": {"events": []}},
    }
    for key in ("state", "events"):
        result[key]["source_times"] = {
            "observed_at": at.isoformat(),
            "collected_at": at.isoformat(),
        }
    if include_metrics:
        result["metrics"] = metrics(start, end, pod_uid="pod-0", error=0.3 if replicas == 1 else 0)
        if replicas == 2:
            second = metrics(start, end, pod_uid="pod-1")
            for key in ("requests", "sample_time", "resets"):
                result["metrics"][key]["series"].extend(second[key]["series"])
    return result


def start(c, monkeypatch, *, already_recovered=False):
    plan = propose(c)
    identifier = approve(c, plan)

    def capture(_id, _incident, workload, start, end, **kwargs):
        value = controlled_capture(
            c,
            workload,
            datetime.now(timezone.utc),
            replicas=1,
            start=start,
            end=end,
            include_metrics=True,
        )
        if already_recovered:
            value["metrics"]["error_ratio"]["series"][0]["samples"] = [
                [stamp, 0] for stamp, _ in value["metrics"]["error_ratio"]["series"][0]["samples"]
            ]
        return value

    monkeypatch.setattr(verification.sources, "capture", capture)
    calls = []

    def gateway(path, request):
        calls.append(request)
        return {
            "schema_version": "RecoveryReceipt/v2",
            "operation_id": request["operation_id"],
            "plan_id": request["plan_id"],
            "plan_digest": request["plan_digest"],
            "attribution": "acknowledged",
            "code": "acknowledged",
            "reason": "Controlled acknowledgement",
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
            "acknowledged_resource_version": "controlled-ack-rv",
        }

    monkeypatch.setattr(service.client, "gateway", gateway)
    handler = service.ExecutionHandler()
    claim = claim_next({"recovery_execution_v1": handler}, GATE)
    assert claim and claim.subject_id == identifier
    result = service.execute(identifier, before_call=lambda: None)
    assert finish(claim, handler, result=result, gate=GATE)
    return identifier, calls


def test_fresh_manual_plan_activates_only_with_its_exact_profile_and_old_preview_stays_unavailable(
    qualified, monkeypatch
):
    c = qualified
    plan = propose(c)
    assert plan["content"]["execution_capability"] == "conditional_dispatch_v2"
    assert plan["content"]["verification_profile_digest"] == c.verification_profile.digest()
    with sessions()() as db, db.begin():
        row = db.get(ActionPlan, UUID(plan["id"]))
        original = copy.deepcopy(row.content)
        row.content = {**row.content, "execution_capability": "unavailable"}
    actor = c.actors["operator"]
    assert not actor.client.get(f"/api/plans/{plan['id']}").json()["valid"]
    with sessions()() as db, db.begin():
        db.get(ActionPlan, UUID(plan["id"])).content = original
    monkeypatch.setattr(verification_policy, "load_for", lambda *_args: None)
    assert not actor.client.get(f"/api/plans/{plan['id']}").json()["valid"]


def test_unavailable_optional_qualification_preserves_legacy_manual_downscale(
    candidate, monkeypatch
):
    c = candidate
    monkeypatch.delenv("RECOVERY_VERIFICATION_FILE", raising=False)
    deployment = c.consumer.deployment
    deployment["spec"]["replicas"] = 2
    deployment["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "explicit-api-token",
            "projected": {
                "sources": [{"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}]
            },
        }
    ]
    actor = c.actors["operator"]
    response = actor.client.post(
        f"/api/incidents/{c.consumer.incident_id}/plans",
        headers=actor.headers,
        json={
            "origin": "operator_manual",
            "replicas": 1,
            "rationale": "Explicit legacy manual downscale; no qualified verification configured.",
        },
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["origin"] == "operator_manual" and value["content"]["replicas"] == 1
    assert value["content"].get("schema_version") != "RecoveryPlan/v2"


def test_already_recovered_target_is_no_op_without_gateway_call(qualified, monkeypatch):
    identifier, calls = start(qualified, monkeypatch, already_recovered=True)
    assert calls == []
    with sessions()() as db:
        row = db.get(Execution, identifier)
        assert row.state == "no_op" and row.dispatch_started_at is None and not row.holds_lock
        assert not db.scalar(select(Job.id).where(Job.kind == VERIFICATION_JOB))


def test_acknowledgement_starts_fixed_clocked_slots_and_first_finalization_is_immutable(
    qualified, monkeypatch
):
    c = qualified
    actor, viewer = c.actors["operator"], c.actors["viewer"]
    path = f"/api/incidents/{c.incident_id}"
    resolved = actor.client.post(
        path + "/lifecycle",
        headers=actor.headers,
        json={
            "action": "resolve",
            "reason": "Retain a real frozen handoff during a separate recovery.",
        },
    )
    assert resolved.status_code == 200, resolved.text
    handoff = viewer.client.get(path + "/handoff").content
    report_path = f"/api/recommendation-reports/{c.consumer.report_id}/export"
    report = viewer.client.get(report_path).content
    retained = snapshot((InvestigationRun, Evidence, RecommendationReport, IncidentActivity))
    identifier, calls = start(c, monkeypatch)
    assert len(calls) == 1
    with sessions()() as db:
        row = db.get(Execution, identifier)
        record = db.get(Verification, identifier)
        assert row.state == "verifying" and row.holds_lock and row.outcome.verification == "pending"
        anchor, deadline = record.acknowledged_at, record.deadline
        assert (deadline - anchor).total_seconds() == 180
    clock = [anchor]
    monkeypatch.setattr(verification, "database_now", lambda db: clock[0])

    def capture(_id, _incident, workload, start, end, **kwargs):
        return controlled_capture(
            c,
            workload,
            clock[0],
            replicas=2,
            start=start + timedelta(seconds=60),
            end=end,
            include_metrics=kwargs.get("profile") is not None,
        )

    monkeypatch.setattr(verification.sources, "capture", capture)
    for slot in range(7):
        clock[0] = anchor + timedelta(seconds=slot * 30)
        with sessions()() as db, db.begin():
            db.scalar(
                select(Job).where(Job.kind == VERIFICATION_JOB, Job.subject_id == identifier)
            ).due_at = database_now(db) - timedelta(seconds=1)
        claim = claim_next({VERIFICATION_JOB: verification.VerificationHandler()}, GATE)
        assert claim is not None
        verification.verify(claim, GATE, threading.Event())
        with sessions()() as db:
            record = db.get(Verification, identifier)
            assert (record.acknowledged_at, record.deadline) == (anchor, deadline)
            assert record.next_slot == slot + 1 and len(record.samples) == slot + 1
    with sessions()() as db, db.begin():
        row = db.get(Execution, identifier)
        record = db.get(Verification, identifier)
        assert row.state == "succeeded", record.result
        assert record.result["application"]["coverage"] == 1
        first = record.finalized_at
        verification.finalize(db, row, record, {"state": "inconclusive"})
        assert record.finalized_at == first and row.state == "succeeded"
    for role in ("operator", "viewer"):
        response = c.actors[role].client.get(f"/api/executions/{identifier}/verification")
        assert response.status_code == 200 and response.json()["result"]["state"] == "passed"
    assert (
        snapshot((InvestigationRun, Evidence, RecommendationReport, IncidentActivity)) == retained
    )
    assert viewer.client.get(path + "/handoff").content == handoff
    assert viewer.client.get(report_path).content == report


VERIFY_PROCESS = """
import pathlib,sys,time
from cloudops.entrypoints import worker
from cloudops.features.recovery import verification
from cloudops.features.recovery.verification_policy import VerificationProfile
mode,marker=sys.argv[1],pathlib.Path(sys.argv[2])
verification.require_plan=lambda content:VerificationProfile.model_validate(content['verification_profile'])
def capture(*args,**kwargs):
    assert mode=='pause','A lost slot cannot be collected twice'
    marker.write_text('durable slot reserved; controlled source entered')
    time.sleep(60)
verification.sources.capture=capture
worker.promote_deferred=lambda **kwargs:None
assert worker.run_once()
"""


def test_killed_real_worker_preserves_slot_deadline_and_read_budget(
    qualified, monkeypatch, tmp_path
):
    identifier, calls = start(qualified, monkeypatch)
    marker = tmp_path / "verification-cut"
    with sessions()() as db:
        row = db.get(Verification, identifier)
        anchor, deadline = row.acknowledged_at, row.deadline
    with (tmp_path / "worker.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", VERIFY_PROCESS, "pause", str(marker)],
            env=os.environ.copy(),
            stdout=log,
            stderr=log,
        )
        try:
            until = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < until:
                time.sleep(0.05)
            if not marker.exists():
                log.seek(0)
                pytest.fail("Verification cutpoint unavailable: " + log.read()[-1500:])
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    with sessions()() as db, db.begin():
        row = db.get(Verification, identifier)
        job = db.scalar(
            select(Job).where(Job.kind == VERIFICATION_JOB, Job.subject_id == identifier)
        )
        assert row.next_slot == 1 and row.samples == {}
        assert (row.acknowledged_at, row.deadline) == (anchor, deadline)
        maximum = job.max_attempts
        job.lease_until = database_now(db) - timedelta(seconds=1)
    result = subprocess.run(
        [sys.executable, "-c", VERIFY_PROCESS, "no-call", str(marker)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    with sessions()() as db:
        row = db.get(Verification, identifier)
        job = db.scalar(
            select(Job).where(Job.kind == VERIFICATION_JOB, Job.subject_id == identifier)
        )
        assert row.next_slot == 1 and row.samples == {}
        assert (row.acknowledged_at, row.deadline) == (anchor, deadline)
        assert job.state == "queued" and job.attempts == 2 and job.max_attempts == maximum
        assert job.due_at == anchor + timedelta(seconds=30)
    assert len(calls) == 1


def test_expired_fixed_window_finalizes_inconclusive_without_another_source_read(
    qualified, monkeypatch
):
    identifier, calls = start(qualified, monkeypatch)
    with sessions()() as db:
        deadline = db.get(Verification, identifier).deadline
    monkeypatch.setattr(verification, "database_now", lambda db: deadline + timedelta(seconds=11))

    def unexpected(*args, **kwargs):
        raise AssertionError("Expired verification cannot spend another read budget.")

    monkeypatch.setattr(verification.sources, "capture", unexpected)
    claim = claim_next({VERIFICATION_JOB: verification.VerificationHandler()}, GATE)
    verification.verify(claim, GATE, threading.Event())
    with sessions()() as db:
        row = db.get(Execution, identifier)
        saved = db.get(Verification, identifier)
        assert row.state == "manual_intervention" and row.outcome.verification == "inconclusive"
        assert saved.deadline == deadline and saved.next_slot == 0
    assert len(calls) == 1


def test_verification_migration_preserves_legacy_bytes_and_guards_retained_evidence(
    permission_context,
):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    identifier = UUID(permission_context.consumer.execution_id)
    with sessions()() as db:
        execution = db.get(Execution, identifier)
        plan = db.get(ActionPlan, execution.plan_id)
        before = copy.deepcopy((execution.result, plan.content, plan.digest))
    command.downgrade(config, "0014_recovery_lifecycle")
    command.upgrade(config, "head")
    with sessions()() as db, db.begin():
        execution = db.get(Execution, identifier)
        plan = db.get(ActionPlan, execution.plan_id)
        assert (execution.result, plan.content, plan.digest) == before
        db.add(
            Verification(
                execution_id=identifier,
                profile=profile().model_dump(mode="json"),
                profile_digest=profile().digest(),
                baseline=None,
                samples={},
                result={},
            )
        )
    with pytest.raises(RuntimeError, match="Retained verification"):
        command.downgrade(config, "0014_recovery_lifecycle")
    with sessions()() as db:
        assert db.get(Verification, identifier) is not None
