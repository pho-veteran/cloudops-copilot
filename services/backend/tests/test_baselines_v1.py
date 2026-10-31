"""PostgreSQL + authenticated routes with controlled read-only observations."""

import copy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from test_permissions import permission_context as _permission_context, snapshot

from cloudops.features.access.models import LoginSession
from cloudops.features.access.service import token_hash
from cloudops.features.workloads import baseline_service
from cloudops.features.recovery import baseline_context
from cloudops.features.workloads.baseline_schemas import BaselineEnvelope
from cloudops.features.workloads.models import Baseline, BaselineCandidate, Workload
from cloudops.features.workloads.observation_models import WorkloadObservation
from cloudops.features.workloads.observation_schemas import DirectOutcome
from cloudops.shared.database import database_now, sessions

permission_context = _permission_context


def test_canonical_envelope_whitelist_order_quantities_and_scale_compatibility():
    from observation_fixtures import objects, observed
    from cloudops.features.workloads.kubernetes import snapshot as scale_snapshot
    from cloudops.features.workloads.observation_evaluator import extract

    data = objects()
    deployment = data[0]
    before = scale_snapshot(deployment)
    value = baseline_service.envelope(observed(data=data))
    value.containers.append(value.containers[0].model_copy(update={"name": "another"}))
    from cloudops.features.workloads.observation_schemas import ConfigReference

    value.config_references = [
        ConfigReference(path="z", kind="Secret", name="credentials", key="token"),
        ConfigReference(path="a", kind="ConfigMap", name="settings"),
    ]
    original = baseline_service.digest(value)
    value.containers.reverse()
    value.config_references.reverse()
    assert baseline_service.digest(value) == original
    encoded = baseline_service.canonical(value).decode()
    assert '"cpu":"100m"' in encoded
    for sentinel in (
        "private-env-sentinel",
        "private-args-sentinel",
        "private-annotation-sentinel",
        "private-termination-sentinel",
    ):
        assert sentinel not in encoded
    assert '"kind":"Secret"' in encoded and '"name":"credentials"' in encoded
    assert scale_snapshot(deployment) == before
    deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {
            "name": "REF",
            "valueFrom": {
                "secretKeyRef": {"name": "credential-ref", "key": "password", "optional": False}
            },
        }
    )
    fields, refs, reasons = extract(deployment)
    assert not reasons and refs[0].name == "credential-ref" and fields[0].requests.cpu == "100m"
    value.containers[0].resources.requests.cpu = "0.1"
    assert baseline_service.digest(value) != original


def url(c, suffix):
    return f"/api/workloads/{c.consumer.workload_id}/{suffix}"


def preview(c):
    response = c.actors["operator"].client.get(url(c, "baseline-candidate"))
    assert response.status_code == 200, response.text
    value = response.json()
    with sessions()() as db, db.begin():
        row = db.get(BaselineCandidate, UUID(value["id"]))
        saved = copy.deepcopy(row.snapshot)
        now = database_now(db) - timedelta(seconds=6)
        row.created_at = now
        for key in ("observed_at", "started_at"):
            saved["provenance"]["first"][key] = now.isoformat()
        row.snapshot = saved
    return {"candidate_id": value["id"], "digest": value["digest"]}


def confirm(c, body):
    actor = c.actors["operator"]
    return actor.client.post(url(c, "baseline"), json=body, headers=actor.headers)


def context(c, selected=None, role="operator"):
    return c.actors[role].client.get(
        url(c, "recovery-context"), params={"baseline_id": selected} if selected else {}
    )


def test_confirm_provenance_exact_context_idempotence_and_legacy(permission_context):
    c = permission_context
    before = snapshot()
    assert context(c).json()["reason"] == "missing"
    assert context(c, c.consumer.baseline_id).json()["reason"] == "legacy"
    assert snapshot() == before
    body = preview(c)
    saved = confirm(c, body)
    assert saved.status_code == 201, saved.text
    value = saved.json()
    assert value["profile"] == baseline_service.PROFILE
    first, second = (value["snapshot"]["provenance"][key] for key in ("first", "second"))
    assert first["observation_id"] != second["observation_id"]
    assert first["health"] == second["health"] == "healthy"
    assert "_session_binding" not in saved.text
    supported = BaselineEnvelope.model_validate(
        {k: v for k, v in value["snapshot"].items() if k != "provenance"}
    )
    assert "replicas" not in supported.model_dump()
    original = snapshot()
    again = confirm(c, body)
    assert again.status_code == 201
    for key in ("id", "snapshot", "confirmed_at", "confirmed_by", "digest"):
        assert again.json()[key] == value[key]
    assert snapshot() == original
    for role in ("viewer", "operator"):
        result = context(c, value["id"], role).json()
        assert result["reason"] == "no_supported_diff" and not result["execution_eligible"]
        assert result["baseline"]["snapshot"] == value["snapshot"]
    c.consumer.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "fixture:changed"
    result = context(c, value["id"]).json()
    assert result["state"] == "baseline_context" and result["reason"] == "restore_not_implemented"
    assert result["supported_diff"] == [
        {
            "path": "containers.consumer.image",
            "current": "fixture:changed",
            "baseline": "example/fixture:1",
        }
    ]
    assert result["baseline"]["id"] == value["id"] and not result["execution_eligible"]
    with sessions()() as db, db.begin():
        db.execute(delete(WorkloadObservation))
    assert context(c, value["id"]).json()["baseline"]["snapshot"] == value["snapshot"]
    after = snapshot()
    for table in before.keys() - {"baseline_candidates", "baselines", "workload_observations"}:
        assert before[table] == after[table], table


@pytest.mark.parametrize(
    "change",
    [
        "uid",
        "epoch",
        "generation",
        "image",
        "restart",
        "reset",
        "pod_set",
        "references",
        "partial",
        "unhealthy",
        "denied",
        "expired",
        "foreign",
        "digest",
    ],
)
def test_changed_or_unqualified_second_read_cannot_confirm(permission_context, monkeypatch, change):
    c = permission_context
    body = preview(c)
    original = baseline_service.observe_direct
    if change in {"uid", "epoch"}:
        with sessions()() as db, db.begin():
            workload = db.get(Workload, UUID(c.consumer.workload_id))
            if change == "uid":
                workload.uid = "recreated-uid"
            else:
                workload.observation_epoch += 1
    elif change == "expired":
        with sessions()() as db, db.begin():
            row = db.get(BaselineCandidate, UUID(body["candidate_id"]))
            value = copy.deepcopy(row.snapshot)
            value["provenance"]["first"]["observed_at"] = (
                database_now(db) - timedelta(minutes=6)
            ).isoformat()
            row.snapshot = value
    elif change == "foreign":
        body["candidate_id"] = str(uuid4())
    elif change == "digest":
        body["digest"] = "0" * 64
    if change == "reset":
        with sessions()() as db, db.begin():
            row = db.get(BaselineCandidate, UUID(body["candidate_id"]))
            value = copy.deepcopy(row.snapshot)
            value["provenance"]["first"]["pods"][0]["containers"][0]["restart_count"] = 2
            row.snapshot = value

    async def changed(*args):
        result = await original(*args)
        obs = result.observation.model_copy(deep=True)
        if change == "generation":
            obs.deployment.generation += 1
        if change == "image":
            obs.template_fields[0].image = "changed:2"
        if change == "restart":
            obs.pods[0].containers[0].restart_count += 1
        if change == "pod_set":
            obs.pods[0].uid = "replacement-pod"
        if change == "references":
            from cloudops.features.workloads.observation_schemas import ConfigReference

            obs.config_references = [
                ConfigReference(
                    path="containers.consumer.envFrom", kind="ConfigMap", name="changed"
                )
            ]
        if change == "partial":
            obs.coverage = "partial"
        if change == "unhealthy":
            obs.health = "degraded"
        if change == "denied":
            return DirectOutcome(outcome="failed", reason="permission_denied")
        return DirectOutcome(
            outcome="partial" if change == "partial" else "complete", observation=obs
        )

    monkeypatch.setattr(baseline_service, "observe_direct", changed)
    before = snapshot()
    response = confirm(c, body)
    assert response.status_code == 409, response.text
    assert snapshot() == before


def test_new_session_and_inflight_revocation_cannot_replay(permission_context, monkeypatch):
    c = permission_context
    body = preview(c)
    actor = c.actors["operator"]
    login = actor.client.post(
        "/api/login",
        json={"username": "operator", "password": "local-test-password"},
        headers={"Origin": "http://localhost:5173"},
    )
    actor.session = login.json()
    assert confirm(c, body).status_code == 409
    body = preview(c)
    original = baseline_service.observe_direct

    async def revoke(*args):
        # This commits while the external read is in flight: no account row lock spans I/O.
        with sessions()() as db, db.begin():
            db.execute(
                delete(LoginSession).where(
                    LoginSession.token_hash == token_hash(actor.client.cookies["cloudops_session"])
                )
            )
        return await original(*args)

    monkeypatch.setattr(baseline_service, "observe_direct", revoke)
    before = snapshot()
    assert confirm(c, body).status_code == 401
    assert snapshot() == before


def test_history_pagination_retains_legacy_and_selected_identity(permission_context):
    c = permission_context
    with sessions()() as db, db.begin():
        old = db.get(Baseline, UUID(c.consumer.baseline_id))
        immutable = (old.digest, copy.deepcopy(old.snapshot), old.confirmed_by, old.confirmed_at)
        for _ in range(30):
            candidate = BaselineCandidate(
                workload_id=old.workload_id, digest=old.digest, snapshot=old.snapshot
            )
            db.add(candidate)
            db.flush()
            db.add(
                Baseline(
                    workload_id=old.workload_id,
                    candidate_id=candidate.id,
                    digest=old.digest,
                    snapshot=old.snapshot,
                    confirmed_by=old.confirmed_by,
                )
            )
    client = c.actors["viewer"].client
    first = client.get(url(c, "baselines")).json()
    assert len(first["items"]) == 25 and first["next_cursor"]
    second = client.get(url(c, "baselines"), params={"cursor": first["next_cursor"]}).json()
    rows = first["items"] + second["items"]
    assert len(rows) == len({r["id"] for r in rows}) == 31
    assert all(r["profile"] == "legacy/v0" for r in rows)
    with sessions()() as db:
        old = db.get(Baseline, UUID(c.consumer.baseline_id))
        assert (old.digest, old.snapshot, old.confirmed_by, old.confirmed_at) == immutable
    assert client.get(url(c, "baselines"), params={"cursor": "invalid!"}).status_code == 422


def test_legacy_pending_and_corrupt_or_foreign_recovery_record_fail_closed(permission_context):
    c = permission_context
    with sessions()() as db:
        legacy = db.get(Baseline, UUID(c.consumer.baseline_id))
        legacy_body = {"candidate_id": str(legacy.candidate_id), "digest": legacy.digest}
    before = snapshot()
    assert confirm(c, legacy_body).status_code == 409
    assert snapshot() == before
    saved = confirm(c, preview(c)).json()
    with sessions()() as db, db.begin():
        row = db.get(Baseline, UUID(saved["id"]))
        row.digest = "0" * 64
    before = snapshot()
    assert context(c, saved["id"]).json()["reason"] == "mismatch"
    with sessions()() as db, db.begin():
        row = db.get(Baseline, UUID(saved["id"]))
        other = Workload(
            cluster="other", namespace="demo", name="other", uid="other", source="live_kubernetes"
        )
        db.add(other)
        db.flush()
        row.workload_id = other.id
    before = snapshot()
    assert context(c, saved["id"]).json()["reason"] == "mismatch"
    assert snapshot() == before


@pytest.mark.parametrize(
    "outcome,code", [("busy", "observation_busy"), ("superseded", "observation_superseded")]
)
def test_busy_or_superseded_is_retry_guidance_not_unhealthy(
    permission_context, monkeypatch, outcome, code
):
    c = permission_context

    async def busy(*args):
        return DirectOutcome(
            outcome=outcome, reason="budget_exhausted" if outcome == "busy" else "superseded"
        )

    monkeypatch.setattr(baseline_service, "observe_direct", busy)
    before = snapshot()
    response = c.actors["operator"].client.get(url(c, "baseline-candidate"))
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert "repair manually" not in response.text
    assert snapshot() == before


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("unavailable", "unavailable"),
        ("identity", "mismatch"),
        ("references", "config_unverified"),
        ("containers", "unsupported"),
        ("unsupported_reference", "unsupported"),
    ],
)
def test_actual_recovery_consumer_fails_closed(permission_context, monkeypatch, condition, reason):
    c = permission_context
    saved = confirm(c, preview(c)).json()
    original = baseline_context.observe_direct

    async def change(*args):
        result = await original(*args)
        if condition == "unavailable":
            return DirectOutcome(outcome="failed", reason="permission_denied")
        obs = result.observation.model_copy(deep=True)
        if condition == "identity":
            obs.identity.uid = "new-uid"
        if condition == "containers":
            obs.template_fields[0].name = "other"
        if condition == "references":
            from cloudops.features.workloads.observation_schemas import ConfigReference

            obs.config_references = [
                ConfigReference(path="volumes.config", kind="ConfigMap", name="new-ref")
            ]
        if condition == "unsupported_reference":
            obs.coverage = "partial"
            obs.health = "unknown"
            obs.reasons = ["unsupported_reference"]
        return DirectOutcome(
            outcome="partial" if condition == "unsupported_reference" else "complete",
            observation=obs,
        )

    monkeypatch.setattr(baseline_context, "observe_direct", change)
    before = snapshot()
    result = context(c, saved["id"]).json()
    assert result["reason"] == reason and result["state"] == "manual_only"
    assert result["execution_eligible"] is False
    assert snapshot() == before
