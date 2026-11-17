"""Real PostgreSQL report contracts with explicitly controlled recommendation inputs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
from typing import Literal
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError
import pytest
from sqlalchemy import func, select, text
from test_dispatch_gate import change
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.access.models import User
from cloudops.features.recommendations import service
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.schemas import (
    Coverage,
    Quantity,
    ReportResult,
    ReportWindow,
    ResourceAdvice,
    Unavailable,
    Unset,
)
from cloudops.features.recommendations.worker import ReportHandler
from cloudops.features.workloads.models import Workload
from cloudops.shared.config import settings
from cloudops.shared.database import BACKEND_ROOT, database_now, engine, sessions
from cloudops.shared.jobs import Claim, Job, JobAttempt, claim_next, finish

permission_context = _permission_context
PATH = "/api/recommendation-reports"


def create(c, **updates):
    actor = c.actors["operator"]
    return actor.client.post(
        PATH,
        json={"workload_id": c.consumer.workload_id, "requested_hours": 6, **updates},
        headers=actor.headers,
    )


def controlled_result(row, now):
    identity = service.ReportIdentity.model_validate(row.input_identity)
    identity.containers = identity.containers or ["app"]
    identity.cluster_fingerprint = identity.cluster_fingerprint or "c" * 64
    identity.template_digest = identity.template_digest or "d" * 64
    identity.resources_digest = identity.resources_digest or "e" * 64
    identity.observed_at = now
    profile = service.ReportProfile.model_validate(row.profile).model_copy(
        update={"image_digest": "sha256:" + "b" * 64}
    )
    start = now - timedelta(hours=6)
    resources = []
    for container in identity.containers:
        resources.extend(
            [
                ResourceAdvice(
                    container=container,
                    resource="cpu",
                    state="available",
                    current_request=Quantity(value="0.2", unit="cores"),
                    current_limit=Unset(),
                    observed=Quantity(value="0.1", unit="cores"),
                    statistic="percentile_95",
                    recommended_request=Quantity(value="0.1", unit="cores"),
                    recommended_limit=Unset(),
                    coverage=Coverage(
                        expected_bins=360,
                        covered_bins=360,
                        ratio=1,
                        longest_gap_seconds=0,
                        first_sample_at=start,
                        last_sample_at=now,
                    ),
                ),
                ResourceAdvice(
                    container=container,
                    resource="memory",
                    state="excluded",
                    current_request=Quantity(value="0", unit="bytes"),
                    current_limit=Unset(),
                    recommended_request=Unavailable(reason="recent_oom"),
                    recommended_limit=Unavailable(reason="recent_oom"),
                    reasons=["recent_oom"],
                ),
            ]
        )
    return ReportResult(
        state="available",
        reason="partial_advice",
        source="live_krr",
        identity=identity,
        profile=profile,
        window=ReportWindow(
            kind="exact",
            start=start,
            end=now,
            raw_input_start=start - timedelta(minutes=3),
            raw_input_end=now,
        ),
        resources=resources,
        output_sha256="a" * 64,
        query_digest="f" * 64,
        tool_exit_code=0,
        limitations=["Controlled test input; not a live KRR qualification."],
    )


def test_terminal_export_partial_contract_and_stale_precedence(permission_context, monkeypatch):
    c = permission_context
    created = create(c)
    assert created.status_code == 201, created.text
    report_id = UUID(created.json()["id"])
    viewer = c.actors["viewer"].client
    assert viewer.get(f"{PATH}/{report_id}/export").status_code == 409
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    assert claim.subject_id == report_id
    with sessions()() as db:
        now = database_now(db)
        value = controlled_result(db.get(RecommendationReport, report_id), now)
    assert finish(claim, ReportHandler(), result=value, gate=GATE)
    frozen = viewer.get(f"{PATH}/{report_id}/export")
    assert frozen.status_code == 200
    export = frozen.json()
    assert export["job_phase"] == "finished" and export["attempts"][0]["state"] == "succeeded"
    assert "applicability" not in export and "dispatch_paused" not in export
    values = export["result_v2"]["resources"]
    assert values[0]["recommended_limit"] == {"kind": "unset"}
    assert values[1]["current_request"]["value"] == "0"
    assert values[1]["recommended_request"]["kind"] == "unavailable"
    detail = viewer.get(f"{PATH}/{report_id}").json()
    assert detail["export_sha256"] == hashlib.sha256(frozen.content).hexdigest()
    assert not finish(claim, ReportHandler(), result=value, gate=GATE)
    monkeypatch.setattr(service, "database_now", lambda db: now + timedelta(hours=25))
    monkeypatch.setattr(
        service,
        "capture_identity",
        lambda *a: (_ for _ in ()).throw(AssertionError("Age already proves stale")),
    )
    assert viewer.get(f"{PATH}/{report_id}").json()["applicability"]["state"] == "stale"
    engine().dispose()
    assert viewer.get(f"{PATH}/{report_id}/export").content == frozen.content


def test_admission_concurrency_and_permission_denials(permission_context):
    c = permission_context
    before = None
    with sessions()() as db:
        before = db.scalar(select(func.count()).select_from(Job))
    for actor in ["anonymous", "expired", "viewer"]:
        a = c.actors[actor]
        response = a.client.post(
            PATH, json={"workload_id": c.consumer.workload_id}, headers=a.headers
        )
        assert response.status_code == (403 if actor == "viewer" else 401)
    operator = c.actors["operator"]
    for headers in [
        {"Origin": "http://localhost:5173"},
        operator.headers | {"Origin": "https://foreign.invalid"},
    ]:
        assert (
            operator.client.post(
                PATH, json={"workload_id": c.consumer.workload_id}, headers=headers
            ).status_code
            == 403
        )
    assert create(c, requested_hours=5).status_code == 422
    assert create(c, endpoint="https://foreign.invalid").status_code == 422
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before

    def attempt(_):
        with TestClient(c.app) as client:
            client.cookies.update(operator.client.cookies)
            return client.post(
                PATH, json={"workload_id": c.consumer.workload_id}, headers=operator.headers
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [201, 409]
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before + 1
        row = db.scalar(
            select(RecommendationReport).where(RecommendationReport.schema_version == 2)
        )
        assert row.input_identity["uid"] == db.get(Workload, row.workload_id).uid


def test_queue_full_rolls_back_without_report(permission_context):
    c = permission_context
    with sessions()() as db, db.begin():
        db.add_all(
            [
                Job(kind="development_collection_v1", subject_id=uuid4())
                for _ in range(settings().queue_limit)
            ]
        )
    response = create(c)
    assert response.status_code == 409 and response.json()["error"]["code"] == "queue_full"
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(RecommendationReport)
                .where(RecommendationReport.schema_version == 2)
            )
            == 0
        )


def test_history_reaches_all_rows_keeps_uid_filter_and_exact_old_read(permission_context):
    c = permission_context
    workload_id = UUID(c.consumer.workload_id)
    with sessions()() as db, db.begin():
        original = db.get(Workload, workload_id)
        old = Workload(
            cluster=original.cluster,
            namespace=original.namespace,
            name=original.name,
            uid=str(uuid4()),
            source="live_kubernetes",
            active=False,
        )
        db.add(old)
        db.flush()
        actor = db.scalar(select(User.id).where(User.username == "operator"))
        rows = [
            RecommendationReport(
                workload_id=workload_id,
                created_by=actor,
                state="insufficient_data",
                result={"recommendations": None},
            )
            for _ in range(67)
        ]
        historical = RecommendationReport(workload_id=old.id, created_by=actor, state="unavailable")
        db.add_all([*rows, historical])
        db.flush()
        for row in [*rows, historical]:
            service.freeze_legacy(row)
        ids = {str(r.id) for r in rows} | {c.consumer.report_id}
        historical_id = historical.id
    viewer = c.actors["viewer"].client
    seen, cursor = [], None
    while True:
        page = viewer.get(
            PATH,
            params={
                "limit": 13,
                "workload_id": str(workload_id),
                **({"cursor": cursor} if cursor else {}),
            },
        ).json()
        seen.extend(x["id"] for x in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert viewer.get(PATH, params={"cursor": cursor}).status_code == 422
    assert len(seen) == len(set(seen)) and set(seen) == ids
    assert viewer.get(f"{PATH}/{historical_id}").json()["workload_id"] != str(workload_id)
    assert viewer.get(f"{PATH}/{historical_id}/export").status_code == 200
    assert viewer.get(f"{PATH}/{uuid4()}").status_code == 404
    for cursor in ["broken", "e30=", "W10=", "LTE="]:
        assert viewer.get(PATH, params={"cursor": cursor}).status_code == 422


def test_pause_lost_lease_and_new_attempt_cannot_rewrite_export(
    permission_context, dispatch_permit
):
    c = permission_context
    report_id = UUID(create(c).json()["id"])
    change(dispatch_permit, "enter_recovery")
    detail = c.actors["viewer"].client.get(f"{PATH}/{report_id}").json()
    assert detail["dispatch_paused"] and detail["dispatch_reason"]
    assert claim_next({service.REPORT_JOB: ReportHandler()}, GATE) is None
    change(dispatch_permit, "resume", resume="recommendation")
    first = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db, db.begin():
        db.get(Job, first.id).lease_until = database_now(db) - timedelta(seconds=1)
        value = service.unavailable(db.get(RecommendationReport, report_id))
    assert not finish(first, ReportHandler(), result=value, gate=GATE)
    second = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    assert second.attempt == 2
    assert finish(second, ReportHandler(), result=value, gate=GATE)
    viewer = c.actors["viewer"].client
    frozen = viewer.get(f"{PATH}/{report_id}/export").content
    assert not finish(first, ReportHandler(), result=value, gate=GATE)
    assert viewer.get(f"{PATH}/{report_id}/export").content == frozen
    assert [a["state"] for a in viewer.get(f"{PATH}/{report_id}").json()["attempts"]] == [
        "expired",
        "succeeded",
    ]


class BaselineReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workload_id: UUID
    state: Literal["queued", "running", "available", "insufficient_data", "unavailable"]
    requested_hours: int
    tool_version: str
    result: dict
    reason: str
    created_by: UUID
    created_at: datetime
    finished_at: datetime | None


def test_populated_cutover_bytes_and_old_claim_rejection(client):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with sessions()() as db, db.begin():
        workload = db.scalar(select(Workload))
        actor = db.scalar(select(User.id).where(User.username == "operator"))
        now = database_now(db)
        rows = [
            RecommendationReport(
                workload_id=workload.id,
                created_by=actor,
                state=state,
                result={
                    "z": {"description": "Giữ nguyên 🧪", "a": [None, 0, 1.25]},
                    "recommendations": None,
                },
                reason="Cutover fixture",
                finished_at=now if state == "insufficient_data" else None,
            )
            for state in ["queued", "running", "insufficient_data"]
        ]
        db.add_all(rows)
        db.flush()
        job = Job(
            kind=service.REPORT_JOB,
            subject_id=rows[1].id,
            state="running",
            attempts=1,
            lease_token=uuid4(),
            lease_until=now + timedelta(minutes=5),
        )
        db.add(job)
        db.flush()
        db.add(
            JobAttempt(
                job_id=job.id,
                number=1,
                lease_token=job.lease_token,
                state="running",
                started_at=now,
            )
        )
        old_claim = Claim(job.id, job.kind, job.subject_id, job.lease_token, 1, 3)
        ids = [r.id for r in rows]
    command.downgrade(config, "0016_recovery_inverse")
    try:
        with engine().connect() as conn:
            persisted = conn.execute(text("SELECT * FROM recommendation_reports")).mappings().all()
            golden = {
                r["id"]: JSONResponse(BaselineReport.model_validate(r).model_dump(mode="json")).body
                for r in persisted
            }
        command.upgrade(config, "head")
        for _ in range(2):
            command.upgrade(config, "head")
        with sessions()() as db:
            for identifier in ids:
                row = db.get(RecommendationReport, identifier)
                assert row.export_text.encode() == golden[identifier]
                assert row.export_sha256 == hashlib.sha256(golden[identifier]).hexdigest()
                assert row.result["z"]["description"] == "Giữ nguyên 🧪"
                assert row.schema_version == 1 and row.result_v2 is None
            assert db.get(Job, old_claim.id).state == "cancelled"
        assert not finish(old_claim, ReportHandler(), result={}, gate=GATE)
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_downgrade_guard_retains_new_report(permission_context):
    c = permission_context
    identifier = create(c).json()["id"]
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        command.downgrade(config, "0016_recovery_inverse")
    command.upgrade(config, "head")
    assert c.actors["viewer"].client.get(f"{PATH}/{identifier}").status_code == 200


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e2", "", True])
def test_quantities_reject_missing_or_nonfinite_ambiguity(value):
    with pytest.raises(ValidationError):
        Quantity(value=value, unit="bytes")


def test_inconsistent_coverage_rejected():
    with pytest.raises(ValidationError):
        Coverage(expected_bins=360, covered_bins=1, ratio=1, longest_gap_seconds=0)


def test_publication_rejects_admitted_image_drift_atomically(permission_context):
    c = permission_context
    identifier = UUID(create(c).json()["id"])
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db, db.begin():
        row = db.get(RecommendationReport, identifier)
        row.profile = {**row.profile, "image_digest": "sha256:" + "c" * 64}
        value = controlled_result(row, database_now(db))
    with pytest.raises(ValueError, match="admitted tool/profile"):
        finish(claim, ReportHandler(), result=value, gate=GATE)
    with sessions()() as db:
        assert db.get(RecommendationReport, identifier).export_text is None
        assert db.get(Job, claim.id).state == "running"
    value.profile.image_digest = "sha256:" + "c" * 64
    assert finish(claim, ReportHandler(), result=value, gate=GATE)


def test_untyped_numeric_output_cannot_publish_available_advice(permission_context):
    c = permission_context
    identifier = create(c).json()["id"]
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    raw = {
        "state": "available",
        "reason": "private-injected-value",
        "result": {
            "source": "live_krr",
            "config": {"token": "private-injected-value"},
            "recommendations": [{"cpu": 1}],
        },
    }
    assert finish(claim, ReportHandler(), result=raw, gate=GATE)
    response = c.actors["viewer"].client.get(f"{PATH}/{identifier}/export")
    assert response.status_code == 200 and "private-injected-value" not in response.text
    assert response.json()["result_v2"]["state"] == "unavailable"
    assert response.json()["result_v2"]["resources"] == []


@pytest.mark.parametrize("code,state", [(0, "insufficient_data"), (1, "unavailable")])
def test_old_tool_receipt_preserves_only_unqualified_provenance(permission_context, code, state):
    c = permission_context
    identifier = create(c).json()["id"]
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    raw = {
        "state": state,
        "reason": "ignored untrusted reason",
        "result": {
            "source": "live_krr",
            "tool_exit_code": code,
            "output_sha256": "a" * 64,
            "config": {"token": "ignored-secret"},
        },
    }
    assert finish(claim, ReportHandler(), result=raw, gate=GATE)
    response = c.actors["viewer"].client.get(f"{PATH}/{identifier}")
    assert response.status_code == 200
    assert (
        response.json()["state"] == state and response.json()["result"]["recommendations"] is None
    )
    assert response.json()["result"]["tool_exit_code"] == code
    assert "ignored-secret" not in response.text and "ignored untrusted reason" not in response.text


def retain_current_observation(db, workload, at, changed=None):
    from copy import deepcopy
    from observation_fixtures import objects, observed
    from cloudops.features.workloads.models import ScopeState
    from cloudops.features.workloads.observation_models import ObservationState, WorkloadObservation
    from cloudops.features.workloads.observation_schemas import Identity

    data = objects()
    dep, replicasets, pods = data
    dep["metadata"].update(name=workload.name, namespace=workload.namespace, uid=workload.uid)
    rs = replicasets["items"][0]
    rs["metadata"]["namespace"] = workload.namespace
    rs["metadata"]["ownerReferences"][0]["uid"] = workload.uid
    pod = pods["items"][0]
    pod["metadata"]["namespace"] = workload.namespace
    if changed:
        container = dep["spec"]["template"]["spec"]["containers"][0]
        if changed == "template":
            container["image"] = "fixture:2"
        elif changed == "env":
            container["env"] = [{"name": "MODE", "value": "private-changed-sentinel"}]
        elif changed == "command":
            container["command"] = ["private-changed-sentinel"]
        else:
            container["resources"]["requests"]["cpu"] = "200m"
        rs["metadata"]["uid"] = str(uuid4())
        rs["spec"]["template"] = deepcopy(dep["spec"]["template"])
        pod["metadata"]["ownerReferences"][0]["uid"] = rs["metadata"]["uid"]
        pod["metadata"]["uid"] = str(uuid4())
        pod["spec"] = deepcopy(dep["spec"]["template"]["spec"])
    value = observed(
        Identity(
            workload_id=workload.id,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
            uid=workload.uid,
            epoch=workload.observation_epoch,
        ),
        at,
        data,
    )
    assert value.coverage == "complete"
    row = WorkloadObservation(
        id=value.observation_id,
        workload_id=workload.id,
        epoch=workload.observation_epoch,
        observed_at=at,
        payload=value.model_dump(mode="json"),
        comparison={},
    )
    db.add(row)
    state = db.get(ObservationState, workload.id)
    if state is None:
        state = ObservationState(workload_id=workload.id, next_due_at=at + timedelta(seconds=30))
        db.add(state)
    state.sequence = state.success_sequence = 1
    state.latest_outcome = "complete"
    state.last_attempt_at = state.last_success_at = at
    state.last_success_id = row.id
    state.next_due_at = at + timedelta(seconds=30)
    scope = db.get(ScopeState, 1)
    scope.mode, scope.cluster_key, scope.fingerprint = "v2", workload.cluster, "c" * 64
    db.flush()
    return row, state


@pytest.mark.parametrize("outcome", ["failed", "partial"])
def test_new_source_failure_cannot_reuse_recent_success_as_current(permission_context, outcome):
    from cloudops.features.workloads.models import ScopeState
    from cloudops.features.workloads.observation_models import ObservationState

    c = permission_context
    with sessions()() as db, db.begin():
        workload = db.get(Workload, UUID(c.consumer.workload_id))
        retain_current_observation(db, workload, database_now(db) - timedelta(seconds=10))
    identifier = UUID(create(c).json()["id"])
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db:
        value = controlled_result(db.get(RecommendationReport, identifier), database_now(db))
    assert finish(claim, ReportHandler(), result=value, gate=GATE)
    viewer = c.actors["viewer"].client
    assert viewer.get(f"{PATH}/{identifier}").json()["applicability"]["state"] == "current"
    with sessions()() as db, db.begin():
        state = db.get(ObservationState, UUID(c.consumer.workload_id))
        state.latest_outcome = outcome
        state.last_attempt_at = state.last_failure_at = database_now(db)
        state.last_failure_reason = "cluster_unavailable"
    assert viewer.get(f"{PATH}/{identifier}").json()["applicability"]["state"] == "unknown"
    with sessions()() as db, db.begin():
        db.get(ScopeState, 1).fingerprint = "d" * 64
    assert viewer.get(f"{PATH}/{identifier}").json()["applicability"]["state"] == "stale"


@pytest.mark.parametrize("changed", ["fingerprint", "template", "resources", "env", "command"])
def test_windowless_report_still_discloses_known_identity_drift(permission_context, changed):
    from cloudops.features.workloads.models import ScopeState

    c = permission_context
    with sessions()() as db, db.begin():
        workload = db.get(Workload, UUID(c.consumer.workload_id))
        retain_current_observation(db, workload, database_now(db) - timedelta(seconds=10))
    identifier = UUID(create(c).json()["id"])
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db:
        value = service.unavailable(db.get(RecommendationReport, identifier))
    assert value.window is None and finish(claim, ReportHandler(), result=value, gate=GATE)
    viewer = c.actors["viewer"].client
    frozen = viewer.get(f"{PATH}/{identifier}/export").content
    saved = viewer.get(f"{PATH}/{identifier}").json()["input_identity"]
    assert saved["pod_template_digest"] is not None
    assert viewer.get(f"{PATH}/{identifier}").json()["applicability"]["state"] == "unknown"
    with sessions()() as db, db.begin():
        if changed == "fingerprint":
            db.get(ScopeState, 1).fingerprint = "d" * 64
        else:
            retain_current_observation(
                db, db.get(Workload, UUID(c.consumer.workload_id)), database_now(db), changed
            )
    detail = viewer.get(f"{PATH}/{identifier}")
    assert detail.json()["applicability"]["state"] == "stale"
    assert detail.json()["input_identity"] == saved
    assert "private-changed-sentinel" not in detail.text
    assert viewer.get(f"{PATH}/{identifier}/export").content == frozen


def suppressed_result(row, cpu_state, aggregate):
    identity = service.ReportIdentity.model_validate(row.input_identity).model_copy(
        update={"containers": ["app"]}
    )
    resources = []
    for resource, state in [("cpu", cpu_state), ("memory", "excluded")]:
        resources.append(
            ResourceAdvice(
                container="app",
                resource=resource,
                state=state,
                current_request=Unavailable(reason="current_unknown"),
                current_limit=Unset(),
                recommended_request=Unavailable(reason="history_gap"),
                recommended_limit=Unavailable(reason="history_gap"),
                reasons=["history_gap"],
            )
        )
    return ReportResult(
        state=aggregate,
        reason="controlled_suppressed",
        source="live_krr",
        identity=identity,
        profile=service.ReportProfile.model_validate(row.profile),
        resources=resources,
    )


@pytest.mark.parametrize(
    "resource_state,valid,contradictory",
    [
        ("insufficient_data", "insufficient_data", "excluded"),
        ("unavailable", "unavailable", "excluded"),
        ("unavailable", "unavailable", "insufficient_data"),
    ],
)
def test_nonavailable_precedence_is_enforced_at_publication(
    permission_context, resource_state, valid, contradictory
):
    c = permission_context
    identifier = UUID(create(c).json()["id"])
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db:
        value = suppressed_result(db.get(RecommendationReport, identifier), resource_state, valid)
    invalid = value.model_copy(update={"state": contradictory})
    with pytest.raises(ValidationError, match="precedence"):
        finish(claim, ReportHandler(), result=invalid, gate=GATE)
    with sessions()() as db:
        assert db.get(RecommendationReport, identifier).export_text is None
    assert finish(claim, ReportHandler(), result=value, gate=GATE)


def test_global_tool_failure_outranks_nonavailable_resource_reasons(permission_context):
    c = permission_context
    identifier = UUID(create(c).json()["id"])
    claim = claim_next({service.REPORT_JOB: ReportHandler()}, GATE)
    with sessions()() as db:
        value = suppressed_result(
            db.get(RecommendationReport, identifier), "insufficient_data", "insufficient_data"
        )
    value = value.model_copy(update={"tool_exit_code": 1, "state": "unavailable"})
    assert finish(claim, ReportHandler(), result=value, gate=GATE)
