"""Persisted handoff and lifecycle behavior; future records are labeled fixtures."""

import base64
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from conftest import sign_in
from sqlalchemy import literal, select, text
from test_access_admission import create, incident_body, rerun
from test_permissions import permission_context as _permission_context

from cloudops.entrypoints.dispatch import GATE
from cloudops.features.access.models import User
from cloudops.features.incidents import handoff
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.recovery import service as recovery
from cloudops.features.recovery.classification import classification_expression, classify
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, claim_next, finish

permission_context = _permission_context


def transition(client, headers, incident_id, action, reason):
    return client.post(
        f"/api/incidents/{incident_id}/lifecycle",
        headers=headers,
        json={"action": action, "reason": reason},
    )


def test_saved_and_legacy_bytes_survive_reopen_later_records_and_renderer_change(
    client, monkeypatch
):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    incident_id = created["incident_id"]
    original = "Human note <script>alert(1)</script>\n```\nGiữ nguyên"
    assert (
        client.post(
            f"/api/incidents/{incident_id}/notes", headers=headers, json={"text": original}
        ).status_code
        == 201
    )
    current = client.get(f"/api/incidents/{incident_id}/handoff/preview").json()
    assert (
        client.get(f"/api/incidents/{incident_id}/handoff").content == current["markdown"].encode()
    )
    assert any(item["text"] == original for item in current["snapshot"]["activity"]["items"])
    fenced_note = f"````text\n{original}\n````\n"
    assert fenced_note in current["markdown"]
    stored_note = next(
        item for item in current["snapshot"]["activity"]["items"] if item["text"] == original
    )
    assert stored_note["actor_name"] in current["markdown"]
    assert stored_note["created_at"] in current["markdown"]
    assert stored_note["id"] in current["markdown"]
    assert "<script>" not in current["markdown"].replace(fenced_note, "")
    assert "```json" not in current["markdown"]
    with sessions()() as db:
        workload = db.get(Workload, UUID(current["snapshot"]["incident"]["workload_id"]))
        for field in ("id", "cluster", "namespace", "name", "uid", "source"):
            assert current["snapshot"]["workload"][field] == str(getattr(workload, field))
        assert "health" not in current["snapshot"]["workload"]
        assert "observation" not in current["snapshot"]["workload"]
        for field in ("cluster", "namespace", "name", "uid"):
            assert getattr(workload, field) in current["markdown"]
    result = transition(client, headers, incident_id, "resolve", "Restored by operator")
    assert result.status_code == 200, result.text
    page = client.get(f"/api/incidents/{incident_id}/activity").json()
    resolution = next(item for item in page["items"] if item["kind"] == "resolve")
    url = f"/api/incidents/{incident_id}/handoff"
    params = {"resolution_id": resolution["id"]}
    saved = client.get(url, params=params).content
    preview = client.get(url + "/preview", params=params).json()
    assert preview["schema_version"] == current["schema_version"]
    assert preview["markdown"].encode() == saved
    assert preview["snapshot"]["activity"]["items"][-1]["text"] == "Restored by operator"
    assert preview["snapshot"]["activity"]["items"][-1]["actor_name"] == "operator"
    assert any(a["text"] == original for a in preview["snapshot"]["activity"]["items"])
    assert preview["snapshot"]["findings"]["status"] == "unavailable"
    assert preview["snapshot"]["citations"]["status"] == "unavailable"
    assert preview["snapshot"]["ruled_out_checks"]["status"] == "unavailable"
    assert preview["snapshot"]["unverified_checks"]["status"] == "unavailable"
    assert preview["snapshot"]["impact"]["status"] == "not_recorded"

    legacy = {"incident": {"id": incident_id}, "activity": [original], "limitations": "old"}
    with sessions()() as db, db.begin():
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        row = IncidentActivity(
            incident_id=UUID(incident_id),
            kind="resolve",
            text="Legacy",
            actor_id=actor,
            snapshot=legacy,
        )
        db.add(row)
        db.flush()
        legacy_id = row.id
    # JSONB's stored ordering is the historical renderer's input.
    with sessions()() as db:
        stored_legacy = db.get(IncidentActivity, legacy_id).snapshot
    content = json.dumps(stored_legacy, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", content)), default=0))
    expected_legacy = f"# Incident handoff\n\n{fence}json\n{content}\n{fence}\n".encode()
    assert client.get(url, params={"resolution_id": str(legacy_id)}).content == expected_legacy
    assert transition(client, headers, incident_id, "reopen", "More work").status_code == 200
    assert (
        client.post(
            f"/api/incidents/{incident_id}/notes", headers=headers, json={"text": "Later note"}
        ).status_code
        == 201
    )
    assert rerun(client, headers, incident_id).status_code == 201
    monkeypatch.setattr(handoff, "render", lambda _: "changed renderer")
    assert client.get(url, params=params).content == saved
    assert client.get(url + "/preview", params=params).json()["markdown"].encode() == saved
    assert client.get(url, params={"resolution_id": str(legacy_id)}).content == expected_legacy
    assert (
        client.get(url + "/preview", params={"resolution_id": str(legacy_id)})
        .json()["markdown"]
        .encode()
        == expected_legacy
    )


@pytest.mark.parametrize("schema_version", ["HandoffSnapshot/v1", "HandoffSnapshot/v2"])
def test_current_handoff_discloses_retirement_without_changing_saved_snapshot(
    client, schema_version
):
    headers = sign_in(client)
    incident_id = create(client, headers, incident_body(client)).json()["incident_id"]
    url = f"/api/incidents/{incident_id}/handoff"
    before = client.get(url + "/preview").json()
    assert before["schema_version"] == "HandoffSnapshot/v3"
    assert before["snapshot"]["workload"]["active"] is True
    assert "Workload status: Active" in before["markdown"]
    old_markdown = "# Incident handoff\n\nPrior v2 saved bytes\n"
    with sessions()() as db, db.begin():
        incident = db.get(Incident, UUID(incident_id))
        old = db.get(Workload, incident.workload_id)
        old_id, old_uid = old.id, old.uid
        row = IncidentActivity(
            incident_id=incident.id,
            kind="resolve",
            text="Prior resolution",
            actor_id=incident.created_by,
            snapshot={
                "schema_version": schema_version,
                "snapshot": before["snapshot"],
                "markdown": old_markdown,
            },
        )
        db.add(row)
        old.active = False
        old.retirement_reason = "replaced"
        replacement = Workload(
            cluster=old.cluster,
            namespace=old.namespace,
            name=old.name,
            uid=old.uid + "-replacement",
            active=True,
            source=old.source,
        )
        db.add(replacement)
        db.flush()
        resolution_id, replacement_id, replacement_uid = row.id, replacement.id, replacement.uid

    current = client.get(url + "/preview").json()
    workload = current["snapshot"]["workload"]
    assert (workload["id"], workload["uid"]) == (str(old_id), old_uid)
    assert workload["active"] is False and workload["retirement_reason"] == "replaced"
    assert "Workload status: Retired" in current["markdown"]
    assert "retirement reason: replaced" in current["markdown"]
    assert "does not grant authority over another UID" in current["markdown"]
    assert replacement_uid not in current["markdown"]
    assert client.get(url).content == current["markdown"].encode()
    params = {"resolution_id": str(resolution_id)}
    assert client.get(url, params=params).content == old_markdown.encode()
    saved_preview = client.get(url + "/preview", params=params).json()
    assert saved_preview["schema_version"] == schema_version
    assert saved_preview["markdown"] == old_markdown
    assert saved_preview["snapshot"] == before["snapshot"]
    with sessions()() as db:
        assert db.get(Incident, UUID(incident_id)).workload_id == old_id
        assert db.get(Workload, replacement_id).active is True

    workload.pop("active")
    workload.pop("retirement_reason")
    unknown = handoff.render(current["snapshot"])
    assert "Workload status: Not recorded; retirement reason: Not recorded" in unknown
    assert "Workload status: Active" not in unknown
    workload["active"] = False
    assert "Workload status: Retired; retirement reason: Not recorded" in handoff.render(
        current["snapshot"]
    )


def test_activity_cursor_rejects_non_string_identity(client):
    headers = sign_in(client)
    incident_id = create(client, headers, incident_body(client)).json()["incident_id"]
    payload = json.dumps([incident_id, "2026-11-01T00:00:00+00:00", 1]).encode()
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    response = client.get(f"/api/incidents/{incident_id}/activity", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_preview_auth_selection_and_cursor_errors(client):
    headers = sign_in(client)
    first = create(client, headers, incident_body(client)).json()["incident_id"]
    second = create(client, headers, incident_body(client)).json()["incident_id"]
    assert transition(client, headers, first, "resolve", "Complete").status_code == 200
    first_page = client.get(f"/api/incidents/{first}/activity").json()
    resolution = next(item for item in first_page["items"] if item["kind"] == "resolve")
    note = client.post(
        f"/api/incidents/{second}/notes", headers=headers, json={"text": "note"}
    ).json()
    for path in ("handoff", "handoff/preview"):
        base = f"/api/incidents/{second}/{path}"
        for selected in (resolution["id"], note["id"], str(uuid4())):
            response = client.get(base, params={"resolution_id": selected})
            assert (
                response.status_code == 404
                and response.json()["error"]["code"] == "resolution_not_found"
            )
        assert client.get(base, params={"resolution_id": "not-a-uuid"}).status_code == 422
    assert (
        client.get(f"/api/incidents/{first}/activity", params={"cursor": "!bad"}).json()["error"][
            "code"
        ]
        == "invalid_cursor"
    )
    with sessions()() as db, db.begin():
        db.get(IncidentActivity, UUID(resolution["id"])).snapshot = {"schema_version": "future/v9"}
    response = client.get(
        f"/api/incidents/{first}/handoff/preview", params={"resolution_id": resolution["id"]}
    )
    assert (
        response.status_code == 422
        and response.json()["error"]["code"] == "unsupported_handoff_snapshot"
    )
    sign_in(client, "viewer")
    assert client.get(f"/api/incidents/{first}/handoff/preview").status_code == 200
    assert (
        client.post(
            f"/api/incidents/{first}/notes", headers=headers, json={"text": "denied"}
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get(f"/api/incidents/{first}/handoff/preview").status_code == 401
    assert client.get(f"/api/incidents/{first}/activity").status_code == 401


@pytest.mark.parametrize(
    ("state", "locked", "released"),
    [
        ("queued", True, False),
        ("verifying", True, False),
        ("reconciling", True, False),
        ("outcome_unknown", True, False),
        ("outcome_unknown", False, True),
        ("outcome_unknown", True, True),
        ("outcome_unknown", False, False),
        ("manual_intervention", False, False),
        ("manual_intervention", False, True),
        ("future_unrecognized", False, False),
    ],
)
def test_execution_classification_sql_matches_python(db, state, locked, released):
    expression = classification_expression(literal(state), literal(locked), literal(released))
    assert db.scalar(select(expression)) == classify(state, locked, released)


def test_stored_handoff_and_notes_work_without_provider_requests(client, monkeypatch):
    headers = sign_in(client)
    incident_id = create(client, headers, incident_body(client)).json()["incident_id"]
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError("Provider unavailable")

    # The current backend has no model producer. Deny outbound Python socket
    # creation as well as the existing recovery provider seam.
    from cloudops.features.recovery import client as gateway_client

    monkeypatch.setattr(socket, "create_connection", unavailable)
    monkeypatch.setattr(gateway_client, "gateway", unavailable)
    monkeypatch.setattr(gateway_client, "target", unavailable)
    text_value = "Human-authored: nguyên văn <b>not instructions</b>"
    assert (
        client.post(
            f"/api/incidents/{incident_id}/notes", headers=headers, json={"text": text_value}
        ).status_code
        == 201
    )
    preview = client.get(f"/api/incidents/{incident_id}/handoff/preview")
    assert preview.status_code == 200
    assert any(
        item["text"] == text_value for item in preview.json()["snapshot"]["activity"]["items"]
    )
    assert (
        transition(client, headers, incident_id, "resolve", "No provider required").status_code
        == 200
    )
    activity = client.get(f"/api/incidents/{incident_id}/activity").json()
    resolution = next(item for item in activity["items"] if item["kind"] == "resolve")
    params = {"resolution_id": resolution["id"]}
    saved_preview = client.get(f"/api/incidents/{incident_id}/handoff/preview", params=params)
    saved_export = client.get(f"/api/incidents/{incident_id}/handoff", params=params)
    assert saved_preview.status_code == saved_export.status_code == 200
    with sessions()() as db:
        frozen = db.get(IncidentActivity, UUID(resolution["id"])).snapshot["markdown"].encode()
    assert saved_preview.json()["markdown"].encode() == saved_export.content == frozen
    assert attempts == []


def test_activity_pages_do_not_expand_saved_reports(client):
    headers = sign_in(client)
    iid = UUID(create(client, headers, incident_body(client)).json()["incident_id"])
    large_report = "Stored report fixture\n" * 10000
    with sessions()() as db, db.begin():
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        rows = [
            IncidentActivity(
                incident_id=iid,
                kind="resolve",
                text=f"Resolution {n}",
                actor_id=actor,
                snapshot={
                    "schema_version": handoff.SCHEMA_VERSION,
                    "snapshot": {},
                    "markdown": large_report,
                },
            )
            for n in range(30)
        ]
        db.add_all(rows)
    response = client.get(f"/api/incidents/{iid}/activity")
    page = response.json()
    assert response.status_code == 200 and len(page["items"]) == 25
    assert page["total"] >= 30 and page["next_cursor"]
    assert len(response.content) < 20000
    assert all("snapshot" not in item for item in page["items"])
    selected = page["items"][0]["id"]
    preview = client.get(
        f"/api/incidents/{iid}/handoff/preview", params={"resolution_id": selected}
    )
    assert preview.json()["markdown"] == large_report


def test_metadata_remains_readable_and_non_activity_actors_are_named(client):
    headers = sign_in(client)
    iid = UUID(create(client, headers, incident_body(client)).json()["incident_id"])
    names = ("incident-owner", "run-requester", "plan-owner", "plan-decider")
    with sessions()() as db, db.begin():
        actors = [
            User(username=name, password_hash="unused-fixture-hash", role="viewer")
            for name in names
        ]
        db.add_all(actors)
        db.flush()
        incident = db.get(Incident, iid)
        incident.created_by = actors[0].id
        run = db.scalar(select(InvestigationRun).where(InvestigationRun.incident_id == iid))
        run.requested_by = actors[1].id
        db.add(
            ActionPlan(
                incident_id=iid,
                workload_id=incident.workload_id,
                state="rejected",
                content={"fixture": "stored actor attribution"},
                digest="0" * 64,
                target_epoch="fixture",
                rationale="Stored attribution fixture, not an execution.",
                created_by=actors[2].id,
                decided_by=actors[3].id,
                expires_at=database_now(db) + timedelta(minutes=5),
            )
        )
    preview = client.get(f"/api/incidents/{iid}/handoff/preview").json()
    assert all(name in preview["markdown"] for name in names)
    assert preview["snapshot"]["incident"]["created_at"] in preview["markdown"]
    preview["snapshot"]["incident"]["origin"]["source_id"] = "team<script>&ops"
    rendered = handoff.render(preview["snapshot"])
    assert "<script>" not in rendered
    assert "&ops" in rendered and "&amp;" not in rendered


def test_full_history_evidence_references_and_cross_incident_lock(client):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    iid = UUID(created["incident_id"])
    with sessions()() as db, db.begin():
        incident = db.get(Incident, iid)
        actor = db.scalar(text("SELECT id FROM users WHERE username='operator'"))
        now = database_now(db)
        other = Incident(
            workload_id=incident.workload_id,
            title="Other incident",
            description="",
            created_by=actor,
        )
        db.add(other)
        db.flush()
        for n in range(110):
            db.add(
                IncidentActivity(
                    incident_id=iid,
                    kind="note",
                    text=f"note {n}",
                    actor_id=actor,
                    created_at=now + timedelta(microseconds=n),
                )
            )
        for n in range(30):
            run = InvestigationRun(
                incident_id=iid,
                state="completed",
                requested_by=actor,
                question=f"Question {n}",
                question_source="operator",
            )
            plan = ActionPlan(
                incident_id=iid,
                workload_id=incident.workload_id,
                state="approved",
                content={"fixture": n},
                digest="0" * 64,
                target_epoch="fixture",
                rationale=f"Action {n}",
                created_by=actor,
                expires_at=now + timedelta(minutes=5),
            )
            db.add_all([run, plan])
            db.flush()
            db.add(
                Evidence(
                    run_id=run.id,
                    workload_uid=db.get(Workload, incident.workload_id).uid,
                    source_key="development_fixture",
                    source_kind="fixture",
                    catalog_version="legacy_v1",
                    schema_version=1,
                    payload_kind="fixture",
                    payload_version=1,
                    time_relation="not_captured",
                    coverage={"status": "available", "reason_codes": ["not_captured"]},
                    redaction_policy_version="legacy_v1",
                    expires_at=now + timedelta(days=30),
                    observed_at=now,
                    collected_at=now,
                    sha256="0" * 64,
                    payload={"untrusted": "not a finding"},
                )
            )
            db.add(
                Execution(
                    plan_id=plan.id,
                    workload_id=incident.workload_id,
                    state="manual_intervention",
                    holds_lock=False,
                    result={"fixture": n},
                )
            )
        locked_plan = ActionPlan(
            incident_id=other.id,
            workload_id=incident.workload_id,
            state="approved",
            content={"fixture": "older unknown"},
            digest="1" * 64,
            target_epoch="fixture",
            rationale="Historical unknown",
            created_by=actor,
            expires_at=now + timedelta(minutes=5),
        )
        db.add(locked_plan)
        db.flush()
        lock = Execution(
            plan_id=locked_plan.id,
            workload_id=incident.workload_id,
            state="outcome_unknown",
            holds_lock=True,
            created_at=now - timedelta(days=10),
            result={"outcome": "outcome_unknown"},
        )
        db.add(lock)
        db.flush()
        lock_id, other_id, lock_plan_id = str(lock.id), str(other.id), str(locked_plan.id)
    url = f"/api/incidents/{iid}/handoff/preview"
    data = client.get(url).json()["snapshot"]
    with sessions()() as db:
        evidence = data["evidence"]["items"][0]
        first_run = evidence["run_id"]
        fixture = handoff.FixtureFindings(
            label="Contract fixture; not production findings",
            items=[
                handoff.FixtureFinding(
                    summary="Fixture-only check",
                    status="ruled_out",
                    run_id=first_run,
                    evidence_ids=[evidence["id"]],
                ),
                handoff.FixtureFinding(
                    summary="Still needs verification",
                    status="unverified",
                    run_id=first_run,
                    evidence_ids=[evidence["id"]],
                ),
            ],
        )
        projected = handoff.compose(db, iid, findings_fixture=fixture)
        assert projected["findings"]["label"] == "Contract fixture; not production findings"
        assert projected["ruled_out_checks"]["items"][0]["summary"] == "Fixture-only check"
        assert projected["unverified_checks"]["items"][0]["summary"] == "Still needs verification"
        assert projected["citations"]["status"] == "contract_fixture"
        assert projected["citations"]["items"] == [
            {"run_id": first_run, "evidence_id": evidence["id"]}
        ]
        fixture_markdown = handoff.render(projected)
        assert first_run in fixture_markdown and evidence["id"] in fixture_markdown
        assert "Fixture-only check" in fixture_markdown
        assert "Still needs verification" in fixture_markdown
        assert "Contract fixture; not production findings" in fixture_markdown
        invalid = fixture.model_copy(
            update={"items": [fixture.items[0].model_copy(update={"evidence_ids": [uuid4()]})]}
        )
        with pytest.raises(ValueError, match="outside its stored Run"):
            handoff.compose(db, iid, findings_fixture=invalid)
    assert data["runs"]["count"] == 31
    assert data["evidence"]["count"] == 30
    assert data["actions"]["plan_count"] == 31
    assert data["actions"]["incident_plan_count"] == 30
    plans = {item["id"]: item for item in data["actions"]["plans"]}
    assert all(item["plan_id"] in plans for item in data["actions"]["executions"])
    assert plans[lock_plan_id]["incident_id"] == other_id
    assert data["target_locks"]["items"][0]["plan_id"] == lock_plan_id
    markdown = client.get(url).json()["markdown"]
    assert markdown.count("Coverage: Not captured (legacy)") == 30
    assert "Coverage: available" not in markdown
    assert lock_id in markdown and lock_plan_id in markdown and other_id in markdown
    assert "Historical unknown" in markdown
    assert data["actions"]["execution_count"] == 31
    assert data["activity"]["count"] >= 110
    run_ids = {r["id"] for r in data["runs"]["items"]}
    assert all(e["run_id"] in run_ids for e in data["evidence"]["items"])
    assert data["findings"]["status"] == "unavailable"
    assert data["target_locks"]["items"][0]["id"] == lock_id
    assert data["target_locks"]["items"][0]["incident_id"] == other_id
    assert "not a finding" not in client.get(url).json()["markdown"]
    collected = []
    cursor = None
    while True:
        response = client.get(
            f"/api/incidents/{iid}/activity",
            params={"limit": 25, **({"cursor": cursor} if cursor else {})},
        )
        page = response.json()
        collected.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(collected) == page["total"] == data["activity"]["count"]
    assert len({item["id"] for item in collected}) == len(collected)
    assert all(item["actor_name"] == "operator" for item in collected)
    assert (
        transition(client, headers, iid, "resolve", "Complete with retained other lock").status_code
        == 200
    )
    selected = next(
        item
        for item in client.get(f"/api/incidents/{iid}/activity").json()["items"]
        if item["kind"] == "resolve"
    )
    for n in range(110):
        assert (
            client.post(
                f"/api/incidents/{iid}/notes", headers=headers, json={"text": f"later {n}"}
            ).status_code
            == 201
        )
    cursor = None
    found = False
    while True:
        page = client.get(
            f"/api/incidents/{iid}/activity",
            params={"limit": 25, **({"cursor": cursor} if cursor else {})},
        ).json()
        found |= any(item["id"] == selected["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert found
    assert (
        client.get(url, params={"resolution_id": selected["id"]}).json()["snapshot"][
            "target_locks"
        ]["items"][0]["id"]
        == lock_id
    )


def test_actual_execution_producer_unknown_and_sql_classification(permission_context):
    c = permission_context
    execution_id = UUID(c.consumer.execution_id)
    incident_id = c.consumer.incident_id
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id)
        row.state, row.holds_lock, row.maintenance_released = "queued", True, False
        db.add(
            Job(
                kind="recovery_execution_v1",
                subject_id=execution_id,
                state="queued",
                max_attempts=2,
            )
        )
    op = c.actors["operator"]
    endpoint = f"/api/incidents/{incident_id}/lifecycle"
    body = {"action": "resolve", "reason": "Controlled producer result"}
    assert op.client.post(endpoint, headers=op.headers, json=body).status_code == 409
    handler = recovery.ExecutionHandler()
    first = claim_next({"recovery_execution_v1": handler}, GATE)
    assert first is not None and first.subject_id == execution_id
    assert op.client.post(endpoint, headers=op.headers, json=body).status_code == 409
    assert finish(first, handler, error="first attempt failed", gate=GATE)
    # failed(terminal=False) produces unknown while the Job is still retryable.
    assert op.client.post(endpoint, headers=op.headers, json=body).status_code == 409
    second = claim_next({"recovery_execution_v1": handler}, GATE)
    assert second is not None and second.subject_id == execution_id
    assert op.client.post(endpoint, headers=op.headers, json=body).status_code == 409
    assert finish(second, handler, error="retry budget exhausted", gate=GATE)
    with sessions()() as db:
        row = db.get(Execution, execution_id)
        sql_class = db.scalar(
            select(
                classification_expression(
                    Execution.state, Execution.holds_lock, Execution.maintenance_released
                )
            ).where(Execution.id == execution_id)
        )
        assert row.finished_at is None  # failed() producer does not set a completion timestamp
        assert (
            sql_class
            == classify(row.state, row.holds_lock, row.maintenance_released)
            == "terminal_unknown_locked"
        )
    response = op.client.post(endpoint, headers=op.headers, json=body)
    assert response.status_code == 200, response.text
    with sessions()() as db:
        assert db.get(Execution, execution_id).holds_lock


def test_published_unknown_receipt_is_terminal_but_retains_lock(permission_context):
    c = permission_context
    execution_id = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        row = db.get(Execution, execution_id)
        row.state, row.holds_lock, row.maintenance_released = "queued", True, False
        db.add(Job(kind="recovery_execution_v1", subject_id=execution_id))
    handler = recovery.ExecutionHandler()
    claim = claim_next({"recovery_execution_v1": handler}, GATE)
    assert claim is not None and claim.subject_id == execution_id
    assert finish(
        claim,
        handler,
        result={
            "outcome": "outcome_unknown",
            "verification": "inconclusive",
            "reason": "Controlled unavailable executor receipt",
        },
        gate=GATE,
    )
    op = c.actors["operator"]
    response = op.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=op.headers,
        json={"action": "resolve", "reason": "Hand off retained unknown"},
    )
    assert response.status_code == 200, response.text
    with sessions()() as db:
        row = db.get(Execution, execution_id)
        assert row.state == "outcome_unknown" and row.holds_lock


@pytest.mark.parametrize(
    ("job_state", "result", "released", "expected"),
    [
        (None, {"outcome": "outcome_unknown"}, False, 409),
        ("queued", {"outcome": "outcome_unknown"}, False, 409),
        ("running", {"outcome": "outcome_unknown"}, False, 409),
        ("completed", {"outcome": "succeeded"}, False, 409),
        ("failed", {"outcome": "outcome_unknown"}, True, 409),
        (
            "failed",
            {
                "outcome": "outcome_unknown",
                "maintenance_request_id": "00000000-0000-4000-8000-000000000001",
            },
            True,
            200,
        ),
    ],
)
def test_unknown_handoff_requires_final_job_and_matching_producer_proof(
    permission_context, job_state, result, released, expected
):
    c = permission_context
    execution_id = UUID(c.consumer.execution_id)
    with sessions()() as db, db.begin():
        execution = db.get(Execution, execution_id)
        execution.state = "outcome_unknown"
        execution.holds_lock = not released
        execution.maintenance_released = released
        execution.result = result
        if job_state is not None:
            db.add(Job(kind="recovery_execution_v1", subject_id=execution_id, state=job_state))
    op = c.actors["operator"]
    response = op.client.post(
        f"/api/incidents/{c.consumer.incident_id}/lifecycle",
        headers=op.headers,
        json={"action": "resolve", "reason": "Inspect persisted proof"},
    )
    assert response.status_code == expected, response.text


def test_resolve_races_new_run_without_resurrecting_cancelled_job(client):
    headers = sign_in(client)
    created = create(client, headers, incident_body(client)).json()
    iid, original = UUID(created["incident_id"]), UUID(created["run_id"])
    with sessions()() as db, db.begin():
        db.get(InvestigationRun, original).state = "completed"
        db.scalar(select(Job).where(Job.subject_id == original)).state = "completed"
    barrier = Barrier(2)

    def resolving():
        barrier.wait(timeout=10)
        return transition(client, headers, iid, "resolve", "Concurrent resolution")

    def admitting():
        barrier.wait(timeout=10)
        return rerun(client, headers, iid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved, admitted = pool.submit(resolving), pool.submit(admitting)
        response, run_response = resolved.result(timeout=20), admitted.result(timeout=20)
    assert response.status_code == 200, response.text
    assert run_response.status_code in (201, 409), run_response.text
    with sessions()() as db:
        assert db.get(Incident, iid).state == "resolved"
        if run_response.status_code == 201:
            run = db.get(InvestigationRun, UUID(run_response.json()["run_id"]))
            assert run.state == "cancelled" and run.outcome_code == "incident_resolved"
            job = db.scalar(select(Job).where(Job.subject_id == run.id))
            assert job is None or job.state != "queued"
