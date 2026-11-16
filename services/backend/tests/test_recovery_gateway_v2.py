"""Real PostgreSQL, SQLite journal and flock; all Kubernetes observations here are controlled."""

import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from recovery_gateway_fixtures import approve, current, intent
from recovery_gateway_fixtures import (
    gateway_case as _gateway_case,
    executor as _executor,
    permission_context as _permission_context,
)
from test_executor import ready, maintain

from cloudops.features.recovery.authority import initialize
from cloudops.features.recovery.dispatch import RecoveryDispatch
from cloudops.features.recovery.gateway import Gateway, create_gateway_app
from cloudops.features.recovery.models import Execution
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError

gateway_case, executor, permission_context = _gateway_case, _executor, _permission_context


@pytest.mark.parametrize(
    "template", ["restore_deployment_template", "set_memory_profile", "scale_deployment"]
)
def test_closed_forward_and_captured_inverse_patch_durable_retry(gateway_case, template):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve(c, intent(c, template))
    forward = c.gate.dispatch(body)
    assert forward["attribution"] == "acknowledged", forward
    assert forward["operation_id"] == str(body.operation_id) and forward["acknowledged_at"]
    patch = c.provider.patches[-1]
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": c.uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": body.resource_version},
    ]
    paths = {op["path"] for op in patch[2:]}
    expected = {
        "scale_deployment": {"/spec/replicas"},
        "restore_deployment_template": {"/spec/template/spec/containers/0/image"},
        "set_memory_profile": {
            "/spec/template/spec/containers/0/resources/requests/memory",
            "/spec/template/spec/containers/0/resources/limits/memory",
        },
    }[template]
    assert paths == expected
    assert current(c) == body.desired.selected()
    assert c.gate.dispatch(body) == forward and c.provider.writes == 1
    assert c.gate.operation(body.operation_id) == forward and c.provider.writes == 1
    with pytest.raises(DomainError, match="different content"):
        c.gate.dispatch(body.model_copy(update={"plan_version": 2}))
    if template == "set_memory_profile":
        c.rules["memory_profiles"] = []
        c.policy_path.write_text(json.dumps(c.rules))
    inverse = approve(c, intent(c, template, direction="inverse", forward=body))
    rolled_back = c.gate.dispatch(inverse)
    assert rolled_back["attribution"] == "acknowledged", rolled_back
    assert current(c) == body.before.selected()
    assert c.gate.dispatch(inverse) == rolled_back and c.provider.writes == 2


@pytest.mark.parametrize(
    "template",
    ["restore_deployment_template", "set_memory_profile", "scale_deployment", "legacy_scale"],
)
def test_expiry_during_capacity_reads_cannot_admit_a_write(gateway_case, monkeypatch, template):
    from cloudops.features.recovery import gateway
    from test_executor import request

    c = gateway_case
    ready(c.gate, c.uid)
    body = (
        request(c.gate, c.provider, c.uid)
        if template == "legacy_scale"
        else approve(c, intent(c, template))
    )

    class Clock:
        value = datetime.now(timezone.utc)

        @classmethod
        def now(cls, timezone):
            return cls.value

    original = c.provider.capacity_budget

    def capacity_then_expire(deployment, extra):
        original(deployment, extra)
        Clock.value = body.expires_at + timedelta(microseconds=1)

    monkeypatch.setattr(gateway, "datetime", Clock)
    monkeypatch.setattr(c.provider, "capacity_budget", capacity_then_expire)
    with TestClient(create_gateway_app(c.gate, "fixture-key")) as client:
        response = client.post(
            "/dispatch",
            json=body.model_dump(mode="json"),
            headers={"Authorization": "Bearer fixture-key"},
        )
    assert response.status_code == 200
    result = response.json()
    assert c.provider.writes == 0
    assert result["code"] == "plan_expired"
    assert c.gate.operation(body.execution_id) == result
    assert c.gate.target(c.uid)["enabled"]


@pytest.mark.parametrize(
    "change",
    [
        "resource_version",
        "epoch",
        "plan_id",
        "plan_digest",
        "policy",
        "expired",
        "profile_value",
        "cpu",
        "image",
        "container",
    ],
)
def test_changed_or_unapproved_intents_do_not_write(gateway_case, change):
    c = gateway_case
    ready(c.gate, c.uid)
    body = intent(c)
    if change == "resource_version":
        body = body.model_copy(update={"resource_version": "stale"})
    elif change == "epoch":
        body = body.model_copy(update={"epoch": "retired"})
    elif change == "expired":
        body = body.model_copy(
            update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
    elif change == "policy":
        body = body.model_copy(
            update={"policy": body.policy.model_copy(update={"digest": "f" * 64})}
        )
    elif change in ("profile_value", "cpu", "image", "container"):
        data = body.model_dump(mode="json")
        desired = data["desired"]["containers"][0]
        if change == "profile_value":
            desired["resources"]["limits"]["memory"] = "512Mi"
        elif change == "cpu":
            desired["resources"]["requests"]["cpu"] = "50m"
        elif change == "image":
            desired["image"] = "example/fixture:2"
        else:
            data["before"]["containers"][0]["name"] = "foreign"
            desired["name"] = "foreign"
        body = RecoveryDispatch.model_validate(data)
    body = approve(c, body)
    if change in ("plan_id", "plan_digest"):
        body = body.model_copy(update={change: uuid4() if change == "plan_id" else "f" * 64})
    try:
        result = c.gate.dispatch(body)
        assert result["attribution"] == "denied", result
    except DomainError as error:
        assert change == "epoch" and error.code == "target_quarantined"
    assert c.provider.writes == 0


@pytest.mark.parametrize(
    "case",
    [
        "unchanged_memory_growth",
        "restore_surge",
        "terminating",
        "hpa",
        "vpa",
        "missing_controllers",
        "missing_nodes",
        "missing_pods",
        "placement",
    ],
)
def test_candidate_peak_capacity_and_controller_fail_closed(gateway_case, case):
    c = gateway_case
    ready(c.gate, c.uid)
    body = intent(
        c, "restore_deployment_template" if case == "restore_surge" else "set_memory_profile"
    )
    if case in ("unchanged_memory_growth", "restore_surge"):
        c.provider.nodes[0]["status"]["allocatable"]["memory"] = "95Mi"
    elif case == "terminating":
        pod = copy.deepcopy(c.provider.pods[0])
        pod["metadata"]["deletionTimestamp"] = datetime.now(timezone.utc).isoformat()
        pod["spec"]["containers"][0]["resources"]["requests"]["memory"] = "4Gi"
        c.provider.pods.append(pod)
    elif case == "hpa":
        c.provider.hpas = [{"spec": {"scaleTargetRef": {"name": "consumer"}}}]
    elif case == "vpa":
        c.provider.vpas = [{"spec": {"targetRef": {"name": "consumer"}}}]
    elif case == "missing_controllers":
        c.provider.unavailable = "/apis/autoscaling/v2/namespaces/demo/horizontalpodautoscalers"
    elif case.startswith("missing_"):
        c.provider.unavailable = "/api/v1/" + case.removeprefix("missing_")
    else:
        c.provider.value["spec"]["template"]["spec"]["nodeSelector"] = {"unsupported": "placement"}
    result = c.gate.dispatch(approve(c, body))
    assert result["attribution"] == "denied", result
    assert c.provider.writes == 0


@pytest.mark.parametrize(
    "unsafe", ["old_state", "forged_values", "forward_plan", "unknown_forward"]
)
def test_inverse_requires_acknowledged_exact_capture_and_independent_safety(gateway_case, unsafe):
    c = gateway_case
    ready(c.gate, c.uid)
    forward = approve(c, intent(c))
    c.provider.lose_ack = unsafe == "unknown_forward"
    c.gate.dispatch(forward)
    if unsafe == "unknown_forward":
        with sessions()() as db, db.begin():
            db.get(Execution, forward.execution_id).state = "outcome_unknown"
        result, _ = maintain(c.gate, c.uid, "enable_target")
        assert result["status"] == "applied"
    if unsafe == "old_state":
        c.rules["inverse_qualified_states"] = []
        c.policy_path.write_text(json.dumps(c.rules))
    inverse = intent(c, direction="inverse", forward=forward)
    data = inverse.model_dump(mode="json")
    if unsafe == "forged_values":
        data["desired"]["containers"][0]["resources"]["limits"]["memory"] = "192Mi"
    elif unsafe == "forward_plan":
        data["selector"]["forward_plan_id"] = str(uuid4())
    inverse = approve(c, RecoveryDispatch.model_validate(data))
    result = c.gate.dispatch(inverse)
    assert result["attribution"] == "denied", result
    assert c.provider.writes == 1


def test_inverse_expiry_during_capacity_cannot_admit_a_write(gateway_case, monkeypatch):
    from cloudops.features.recovery import gateway

    c = gateway_case
    ready(c.gate, c.uid)
    forward = approve(c, intent(c, "scale_deployment"))
    assert c.gate.dispatch(forward)["attribution"] == "acknowledged"
    inverse = approve(c, intent(c, "scale_deployment", direction="inverse", forward=forward))

    class Clock:
        value = datetime.now(timezone.utc)

        @classmethod
        def now(cls, timezone):
            return cls.value

    capacity = c.provider.capacity_budget

    def expire_during_capacity(deployment, extra):
        capacity(deployment, extra)
        Clock.value = inverse.expires_at + timedelta(microseconds=1)

    monkeypatch.setattr(gateway, "datetime", Clock)
    monkeypatch.setattr(c.provider, "capacity_budget", expire_during_capacity)
    result = c.gate.dispatch(inverse)
    assert result["attribution"] == "denied" and result["code"] == "plan_expired"
    assert c.provider.writes == 1 and current(c) == forward.desired.selected()


def test_lost_acknowledgement_lookup_and_restart_never_resend(gateway_case):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve(c, intent(c))
    c.provider.lose_ack = True
    first = c.gate.dispatch(body)
    assert first["attribution"] == "outcome_unknown" and c.provider.writes == 1
    assert current(c) == body.desired.selected()
    assert c.gate.operation(body.operation_id) == first
    assert c.gate.dispatch(body) == first and c.provider.writes == 1
    restarted = Gateway(c.gate.root, kube=c.provider, allowlist=[vars(c.gate.workloads[c.uid])])
    assert not restarted.target(c.uid)["enabled"]
    assert restarted.dispatch(body) == first and c.provider.writes == 1
    with pytest.raises(DomainError, match="absence does not prove"):
        restarted.operation(uuid4())


def test_missing_or_replaced_pin_journal_and_lock_are_never_reinitialized(gateway_case):
    c = gateway_case
    c.gate.identity_file.unlink()
    with pytest.raises(RuntimeError, match="missing or changed"):
        Gateway(c.gate.root, kube=c.provider, allowlist=[vars(c.gate.workloads[c.uid])])
    with pytest.raises(RuntimeError, match="explicit matching legacy"):
        initialize(c.gate.root)
    with pytest.raises(RuntimeError, match="already initialized"):
        initialize(c.gate.root, legacy_id=c.gate.identity["deployment_id"])
    assert c.provider.writes == 0


def test_http_union_authentication_and_read_only_receipts(gateway_case):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve(c, intent(c))
    with TestClient(create_gateway_app(c.gate, "controlled-key")) as client:
        assert client.post("/dispatch", json=body.model_dump(mode="json")).status_code == 403
        for data in (
            {**body.model_dump(mode="json"), "schema_version": "RecoveryDispatch/v3"},
            {**body.model_dump(mode="json"), "arbitrary_patch": []},
        ):
            assert (
                client.post(
                    "/dispatch", json=data, headers={"Authorization": "Bearer controlled-key"}
                ).status_code
                == 422
            )
        assert c.provider.writes == 0
        response = client.post(
            "/dispatch",
            json=body.model_dump(mode="json"),
            headers={"Authorization": "Bearer controlled-key"},
        )
        assert response.status_code == 200 and response.json()["attribution"] == "acknowledged"
        path = f"/operations/{body.operation_id}"
        assert client.get(path).status_code == 403
        assert (
            client.get(path, headers={"Authorization": "Bearer controlled-key"}).json()
            == response.json()
        )
        assert (
            client.get(
                f"/operations/{uuid4()}", headers={"Authorization": "Bearer controlled-key"}
            ).status_code
            == 404
        )
        assert c.provider.writes == 1


@pytest.mark.parametrize("direction", ["forward", "inverse"])
def test_crash_after_admission_preserves_typed_unknown_and_captured_request(
    gateway_case, monkeypatch, direction
):
    c = gateway_case
    ready(c.gate, c.uid)
    body = approve(c, intent(c))
    if direction == "inverse":
        assert c.gate.dispatch(body)["attribution"] == "acknowledged"
        body = approve(c, intent(c, direction="inverse", forward=body))
    writes_before = c.provider.writes
    original = c.provider.request

    def crash(path, *, patch=None, **kwargs):
        if patch and any(op["path"].startswith("/spec/") for op in patch[2:]):
            raise RuntimeError("Controlled process interruption after journal admission")
        return original(path, patch=patch, **kwargs)

    monkeypatch.setattr(c.provider, "request", crash)
    with pytest.raises(RuntimeError, match="Controlled process interruption"):
        c.gate.dispatch(body)
    with c.gate.connection() as db:
        captured = dict(
            db.execute("SELECT * FROM attempts WHERE id=?", (str(body.operation_id),)).fetchone()
        )
    restarted = Gateway(c.gate.root, kube=c.provider, allowlist=[vars(c.gate.workloads[c.uid])])
    result = restarted.operation(body.operation_id)
    assert (
        result["schema_version"] == "RecoveryReceipt/v2"
        and result["attribution"] == "outcome_unknown"
    )
    assert restarted.dispatch(body) == result and c.provider.writes == writes_before
    with restarted.connection() as db:
        stored = dict(
            db.execute("SELECT * FROM attempts WHERE id=?", (str(body.operation_id),)).fetchone()
        )
    assert stored["request"] == captured["request"] and stored["digest"] == captured["digest"]


@pytest.mark.parametrize("missing", ["journal", "lock", "identity_value"])
def test_missing_authority_artifacts_keep_admission_closed(gateway_case, missing):
    c = gateway_case
    if missing == "journal":
        c.gate.path.unlink()
    elif missing == "lock":
        path = c.gate.root / "admission.lock"
        with path.open("rb"):
            path.unlink()
            path.touch()
    else:
        value = json.loads(c.gate.identity_file.read_text())
        value["deployment_id"] = str(uuid4())
        c.gate.identity_file.write_text(json.dumps(value))
    with pytest.raises((RuntimeError, DomainError)):
        Gateway(c.gate.root, kube=c.provider, allowlist=[vars(c.gate.workloads[c.uid])])
    assert c.provider.writes == 0


def test_legacy_journal_upgrade_preserves_exact_requests_digests_and_receipts(executor):
    from test_executor import request

    gate, provider, uid = executor
    ready(gate, uid)
    body = request(gate, provider, uid)
    receipt = gate.dispatch(body)
    with gate.connection() as db:
        retained = tuple(
            db.execute(
                "SELECT request,digest,state,result FROM attempts WHERE id=?",
                (str(body.execution_id),),
            ).fetchone()
        )
        db.execute("DELETE FROM control WHERE key IN ('schema_version','identity_required')")
    original_id = gate.identity["deployment_id"]
    gate.identity_file.unlink()
    initialize(gate.root, legacy_id=original_id)
    restarted = Gateway(gate.root, kube=provider, allowlist=[vars(gate.workloads[uid])])
    with restarted.connection() as db:
        assert (
            tuple(
                db.execute(
                    "SELECT request,digest,state,result FROM attempts WHERE id=?",
                    (str(body.execution_id),),
                ).fetchone()
            )
            == retained
        )
    assert restarted.dispatch(body) == receipt and provider.writes == 1


def test_no_op_is_not_sent_and_never_remediation_success(gateway_case):
    c = gateway_case
    ready(c.gate, c.uid)
    body = intent(c, "scale_deployment")
    body = body.model_copy(
        update={
            "desired": body.before,
            "selector": body.selector.model_copy(update={"replicas": body.before.replicas}),
        }
    )
    body = approve(c, body)
    result = c.gate.dispatch(body)
    assert result["attribution"] == "not_sent" and result["code"] == "no_op"
    assert c.gate.dispatch(body) == result and c.provider.writes == 0


@pytest.mark.parametrize(
    "change",
    ["ttl", "missing_decision", "legacy_preview", "retired_scope", "resolved", "changed_uid"],
)
def test_durable_approval_and_scope_boundaries(gateway_case, change):
    from cloudops.features.recovery.models import ActionPlan
    from cloudops.features.workloads.models import Workload
    from cloudops.features.incidents.models import Incident

    c = gateway_case
    ready(c.gate, c.uid)
    body = intent(c)
    if change == "ttl":
        body = body.model_copy(
            update={"expires_at": datetime.now(timezone.utc) + timedelta(days=1)}
        )
    body = approve(c, body)
    with sessions()() as db, db.begin():
        row = db.get(ActionPlan, body.plan_id)
        if change == "missing_decision":
            row.decided_by = None
        elif change == "legacy_preview":
            row.content = {**row.content, "execution_capability": "unavailable"}
        elif change == "retired_scope":
            db.get(Workload, row.workload_id).active = False
        elif change == "resolved":
            db.get(Incident, row.incident_id).state = "resolved"
    if change == "changed_uid":
        c.provider.value["metadata"]["uid"] = str(uuid4())
    result = c.gate.dispatch(body)
    assert result["attribution"] == "denied", result
    assert c.provider.writes == 0


def test_transaction_start_timestamp_does_not_extend_or_shorten_immutable_ttl(gateway_case):
    from cloudops.features.recovery.models import ActionPlan

    c = gateway_case
    ready(c.gate, c.uid)
    body = approve(c, intent(c))
    with sessions()() as db, db.begin():
        db.get(ActionPlan, body.plan_id).created_at -= timedelta(seconds=20)
    assert c.gate.dispatch(body)["attribution"] == "acknowledged"
