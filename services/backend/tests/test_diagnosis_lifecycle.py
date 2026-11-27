"""PostgreSQL lifecycle tests with a real compatible loopback HTTP server."""

import json
import os
import threading
from pathlib import Path
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from diagnosis_fake import respond, serve
from diagnosis_fixtures import profile, text_payload
from diagnosis_fixtures import refusal_payload
from sqlalchemy import func, select, text
from test_diagnosis_validation import _crashloop, _raw, context as _context, policy

from conftest import sign_in
from cloudops.entrypoints.dispatch import GATE, CLASSES
from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, admit
from cloudops.features.diagnosis.models import (
    Analysis,
    AnalysisContext,
    AnalysisResult,
    AnalysisCall,
    ModelCheck,
)
from cloudops.features.diagnosis.service import analysis_view, model_status
from cloudops.features.diagnosis.settings import Deployment, load
from cloudops.features.diagnosis.slot import ModelSlot, GuardLost
from cloudops.features.investigations.collector import Interrupted
from cloudops.features.diagnosis.worker import _prepare, _reserve, analyze
from cloudops.features.investigations.models import EvidencePin, InvestigationRun
from cloudops.shared.database import database_now
from cloudops.shared.dispatch_permit import transition
from cloudops.shared.jobs import Job, claim_next


context = _context


@pytest.mark.parametrize("field", ["summary", "next_steps", "encoded_summary"])
def test_analysis_rejects_worker_credential_in_advice(client, db, context, deployment, field):
    token = "owned-loopback-test-token"
    _crashloop(db, context)
    payload = _raw(
        **{
            "next_steps" if field == "next_steps" else "summary": (
                ["Inspect " + token] if field == "next_steps" else "Provider echoed " + token
            )
        }
    )
    if field == "encoded_summary":
        payload = payload.replace(token, "".join(f"\\u{ord(char):04x}" for char in token))
    with serve(lambda h: respond(h, text_payload(payload))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        sign_in(client, "viewer")
        response = client.get(f"/api/incidents/{context[0]}/runs/{context[1]}/analysis")
        assert response.status_code == 200
        assert token not in response.text
        assert response.json()["result"] is None
        assert refresh(db, identifier).reason == "credential_in_output"
        assert db.get(AnalysisResult, identifier) is None
        assert db.get(AnalysisContext, identifier).snapshot is None
        assert db.scalar(select(func.count()).select_from(EvidencePin)) == 0
        assert len(server.requests) == 1


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    def install(
        url, *, qualified=True, mode="operational", context_profile="e06", **profile_options
    ):
        model = profile(base_url=url + "/v1", mode=mode, **profile_options)
        egress = policy()
        p, e, q = (
            tmp_path / name for name in ("profile.json", "egress.json", "qualification.json")
        )
        p.write_text(model.model_dump_json(by_alias=True))
        e.write_text(egress.model_dump_json(by_alias=True))
        monkeypatch.setenv("DIAGNOSIS_PROFILE_FILE", str(p))
        monkeypatch.setenv("DIAGNOSIS_EGRESS_FILE", str(e))
        monkeypatch.setenv("DIAGNOSIS_TEST_TOKEN", "owned-loopback-test-token")
        monkeypatch.delenv("DIAGNOSIS_QUALIFICATION_FILE", raising=False)
        if qualified:
            q.write_text(
                json.dumps(
                    {
                        "schema_version": "DiagnosisQualification/v1",
                        "cohort_id": "loopback-fixture-not-provider-quality",
                        "evidence_reference": "pytest",
                        "identity": Deployment(
                            model, egress, None, context_profile=context_profile
                        ).identity(),
                    }
                )
            )
            monkeypatch.setenv("DIAGNOSIS_QUALIFICATION_FILE", str(q))
        return model, egress

    return install


def admission(db, scope):
    run = db.get(InvestigationRun, scope[1])
    analysis = admit(db, run)
    db.commit()
    return analysis.id if analysis else None


def refresh(db, analysis_id):
    db.expire_all()
    return db.get(Analysis, analysis_id)


def capture(db, scope, name):
    destination = os.environ.get("DIAGNOSIS_FIXTURE_OUTPUT")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        payload = analysis_view(db, scope[1]).model_dump(mode="json")
        (path / (name + ".json")).write_text(
            json.dumps(
                {
                    "proof_class": "Real AnalysisView producer over controlled PostgreSQL/loopback fixtures; no live model quality claim.",
                    "analysis": payload,
                    "model_status": model_status(db).model_dump(mode="json"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def test_supported_round_trip_private_context_and_result(db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        capture(db, context, "pending")
        assert run_once()
        analysis = refresh(db, identifier)
        assert analysis.state == "completed", analysis.reason
        assert analysis.calls == 1
        assert db.get(AnalysisResult, identifier).payload["status"] == "supported"
        stored = db.get(AnalysisContext, identifier)
        assert stored.snapshot is None
        assert "request" not in stored.summary
        assert db.scalar(select(func.count()).select_from(EvidencePin)) == 0
        assert db.get(AnalysisContext, identifier).snapshot is None
        assert len(server.requests) == 1
        assert db.get(InvestigationRun, context[1]).state == "completed"
        capture(db, context, "supported")


def test_engine_profile_round_trip_records_engine_versions(db, context, deployment, monkeypatch):
    from cloudops.features.diagnosis.engine import path, selection

    _crashloop(db, context)
    monkeypatch.setenv("DIAGNOSIS_CONTEXT_PROFILE", "engine")
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url, context_profile="engine")
        identifier = admission(db, context)
        assert refresh(db, identifier).identity["context_profile"] == "engine"
        assert run_once()
        analysis = refresh(db, identifier)
        assert analysis.state == "completed", analysis.reason
        result = db.get(AnalysisResult, identifier).payload
        assert result["status"] == "supported"
        assert result["prompt_version"] == path.PROMPT_VERSION
        assert result["context_version"] == selection.CONTEXT_VERSION
        assert len(server.requests) == 1
        sent = json.loads(server.requests[0]["body"])
        assert "DETECTOR HYPOTHESES" in sent["messages"][1]["content"]
    # Rollback: the default profile builds the E06 context again for a new deployment.
    monkeypatch.delenv("DIAGNOSIS_CONTEXT_PROFILE")
    assert load().context_profile == "e06"


def test_terminal_context_does_not_keep_a_copy_after_telemetry_purge(db, context, deployment):
    from cloudops.features.investigations.models import Evidence
    from cloudops.features.investigations.retention import purge_expired

    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        run_once()
        assert refresh(db, identifier).state == "completed"
        manifest = db.get(AnalysisContext, identifier).summary
        for row in db.scalars(select(Evidence).where(Evidence.run_id == context[1])):
            row.expires_at = database_now(db) - timedelta(seconds=1)
        db.commit()
        assert purge_expired(db, apply=True) == 4
        db.expire_all()
        assert all(row.payload is None for row in db.scalars(select(Evidence)))
        stored = db.get(AnalysisContext, identifier)
        assert stored.snapshot is None and stored.summary == manifest
        assert db.get(AnalysisResult, identifier).payload["context_digest"] == stored.digest


def test_unindexed_knowledge_uses_safe_context_without_retry_exhaustion(db, context, deployment):
    from cloudops.features.knowledge.guidance import pin_guidance

    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with db.begin():
            guidance = pin_guidance(
                db,
                context[0],
                context[1],
                consumer="analysis",
                request_identity=str(identifier),
            )
            assert guidance.state == "unavailable" and guidance.selection is None
        assert run_once()
        analysis = refresh(db, identifier)
        stored = db.get(AnalysisContext, identifier)
        job = db.scalar(select(Job).where(Job.kind == ANALYSIS_JOB, Job.subject_id == identifier))
        assert analysis.state == "completed", analysis.reason
        assert analysis.calls == 1 and job.attempts == 1
        assert stored.summary["runbook"]["selection_id"] is None
        assert stored.summary["runbook"]["status"] == "not_selected"
        assert "runbook_not_selected" in stored.summary["limitations"]
        assert stored.snapshot is None
        assert len(server.requests) == 1


@pytest.mark.parametrize("case", ["unqualified", "disabled", "unconfigured"])
def test_unavailable_zero_network(db, context, deployment, monkeypatch, case):
    with serve(lambda h: pytest.fail("No HTTP expected")) as server:
        deployment(
            server.url,
            qualified=case != "unqualified",
            mode="disabled" if case == "disabled" else "operational",
        )
        if case == "unconfigured":
            monkeypatch.delenv("DIAGNOSIS_PROFILE_FILE")
        assert admission(db, context) is None
        capture(db, context, case)
        assert not server.requests
        assert db.get(InvestigationRun, context[1]).outcome_code == "analysis_unavailable"


def test_insufficient_zero_http_and_atomic_unique_admission(db, context, deployment):
    with serve(lambda h: pytest.fail("No HTTP expected")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert admission(db, context) == identifier
        assert run_once()
        assert refresh(db, identifier).state == "insufficient_evidence"
        capture(db, context, "insufficient")
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert not server.requests


@pytest.mark.parametrize("allow", [True, False])
def test_trusted_capture_runs_before_http_and_can_stop_the_send(db, context, deployment, allow):
    from cloudops.features.diagnosis.worker import analyze

    _crashloop(db, context)
    captured = []
    outcomes = []

    def before(prepared, _deployment, request_digest, byte_size):
        assert not server.requests
        assert prepared.run_id == context[1]
        assert byte_size > 0
        captured.append((prepared.digest, request_digest))
        if not allow:
            raise ValueError("test cutoff")

    def after(call, _deployment):
        outcomes.append(call.kind)

    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        other = Job(kind=ANALYSIS_JOB, subject_id=uuid4())
        db.add(other)
        db.commit()
        identifier = admission(db, context)
        assert run_once(
            job_kinds={ANALYSIS_JOB},
            subject_id=identifier,
            run_analysis=lambda *args: analyze(*args, before_request=before, after_request=after),
        )
        db.refresh(other)
        assert other.attempts == 0
        assert len(captured) == 1
        assert len(server.requests) == len(outcomes) == int(allow)
        row = db.get(AnalysisContext, identifier)
        assert row.digest == captured[0][0]
        if allow:
            assert refresh(db, identifier).state == "completed"
            assert db.get(AnalysisResult, identifier).payload["context_digest"] == row.digest
        else:
            assert refresh(db, identifier).calls == 0


def test_repair_reuses_snapshot_one_send_per_claim(db, context, deployment):
    _crashloop(db, context)
    replies = iter(["not json", _raw()])
    with serve(lambda h: respond(h, text_payload(next(replies)))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        first = refresh(db, identifier)
        assert first.calls == first.repairs == 1
        assert first.phase == "repair"
        capture(db, context, "repair-pending")
        digest = db.get(AnalysisContext, identifier).digest
        assert len(server.requests) == 1
        db.commit()
        assert run_once()
        assert refresh(db, identifier).state == "completed"
        assert db.get(AnalysisContext, identifier).digest == digest
        assert len(server.requests) == 2


def test_quota_hold_resume_keeps_budgets_and_deadline(client, db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: respond(h, {}, status=429, headers={"Retry-After": "0"})) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        analysis = refresh(db, identifier)
        assert analysis.wait_mode == "resume_required"
        capture(db, context, "quota-early")
        deadline = analysis.deadline
        assert not run_once()
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        operations_route = "/api/connections/operations"
        waiting = client.get(operations_route).json()["quota_waiting"]
        assert [(item["incident_id"], item["run_id"]) for item in waiting] == [
            (str(context[0]), str(context[1]))
        ]
        assert waiting[0]["earliest_retry_at"] is not None
        route = f"/api/incidents/{context[0]}/runs/{context[1]}/analysis/resume"
        response = client.post(route, headers=headers, json={})
        assert response.status_code == 409 and response.json()["error"]["code"] == "retry_not_due"
        analysis.earliest_retry = database_now(db) - timedelta(seconds=1)
        db.commit()
        still_waiting = client.get(operations_route).json()["quota_waiting"]
        assert [item["run_id"] for item in still_waiting] == [str(context[1])]
        capture(db, context, "quota-eligible")
        db.commit()
        response = client.post(route, headers=headers, json={})
        assert response.status_code == 200, response.text
        resumed = client.get(operations_route).json()
        assert resumed["quota_waiting"] == []
        diagnosis_jobs = next(job for job in resumed["jobs"] if job["kind"] == ANALYSIS_JOB)
        assert diagnosis_jobs["counts"]["due_retry"] == 1
        assert client.post(route, headers=headers, json={}).status_code == 200
        analysis = refresh(db, identifier)
        assert analysis.calls == 1 and analysis.deadline == deadline
        assert len(server.requests) == 1


def test_busy_slot_no_attempts_and_no_call_expiry(db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: pytest.fail("No HTTP expected")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with ModelSlot():
            assert not run_once()
            job = db.scalar(select(Job).where(Job.subject_id == identifier))
            assert job.attempts == 0
            analysis = refresh(db, identifier)
            analysis.deadline = database_now(db) - timedelta(seconds=1)
            db.commit()
            assert not run_once()
            assert refresh(db, identifier).reason == "deadline_expired"
        assert not server.requests


def test_reserved_unsettled_recovery_is_unknown_no_replay(db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: pytest.fail("No HTTP expected")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with ModelSlot() as slot:
            claim = claim_next({ANALYSIS_JOB: HANDLERS[ANALYSIS_JOB]}, GATE)
            snapshot, _, checked = _prepare(claim, GATE, slot)
            _reserve(claim, GATE, slot, checked, threading.Event(), snapshot)
        db.expire_all()
        job = db.get(Job, claim.id)
        job.lease_until = database_now(db) - timedelta(seconds=1)
        db.commit()
        run_once()
        analysis = refresh(db, identifier)
        assert analysis.reason == "provider_outcome_unknown"
        capture(db, context, "unknown-outcome")
        assert analysis.calls == 1
        assert not server.requests


@pytest.mark.parametrize("boundary", ["lease", "permit", "guard"])
def test_lost_authority_cannot_publish(db, context, deployment, dispatch_permit, boundary):
    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with ModelSlot() as slot:
            claim = claim_next({ANALYSIS_JOB: HANDLERS[ANALYSIS_JOB]}, GATE)
            if boundary == "lease":
                db.get(Job, claim.id).lease_until = database_now(db) - timedelta(seconds=1)
                db.commit()
            elif boundary == "permit":
                transition(
                    dispatch_permit,
                    "enter_recovery",
                    CLASSES,
                    reason="Owned test retirement",
                    evidence=["pytest"],
                    actor={},
                )
            else:
                slot.release()
            with pytest.raises(GuardLost if boundary == "guard" else Interrupted):
                analyze(claim, GATE, slot, threading.Event())
        assert db.get(AnalysisResult, identifier) is None
        assert not server.requests


def test_final_attempt_settles_result_without_fourth_claim(db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        identifier = admission(db, context)
        db.scalar(select(Job).where(Job.subject_id == identifier)).attempts = 2
        db.commit()
        assert run_once()
        assert refresh(db, identifier).state == "completed"
        assert not run_once()
        assert len(server.requests) == 1


def test_unresumed_hold_expires_and_releases_pins(db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: respond(h, {}, status=429)) as server:
        deployment(server.url)
        identifier = admission(db, context)
        run_once()
        analysis = refresh(db, identifier)
        assert analysis.state == "waiting_for_quota"
        assert db.scalar(select(func.count()).select_from(EvidencePin)) > 0
        analysis.deadline = database_now(db) - timedelta(seconds=1)
        db.commit()
        with ModelSlot():
            run_once()
        assert refresh(db, identifier).reason == "deadline_expired"
        assert db.get(AnalysisContext, identifier).snapshot is None
        assert db.scalar(select(func.count()).select_from(EvidencePin)) == 0
        assert len(server.requests) == 1


def test_recovery_epoch_quarantines_preexisting_pending(db, context, deployment, dispatch_permit):
    _crashloop(db, context)
    with serve(lambda h: pytest.fail("No replay expected")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        for operation in ("enter_recovery", "exit_recovery"):
            transition(
                dispatch_permit,
                operation,
                CLASSES,
                reason="Owned restore boundary",
                evidence=["pytest"],
                actor={},
            )
            if operation == "enter_recovery":
                status = model_status(db)
                assert status.quality == "qualified"
                assert not status.dispatch_allowed and not status.probe_dispatch_allowed
        run_once()
        assert refresh(db, identifier).reason == "restore_quarantined"
        assert not server.requests


def test_protected_reads_empty_body_and_bounded_probe(client, db, context, deployment):
    with serve(lambda h: respond(h, text_payload("OK") | {"usage": None})) as server:
        deployment(server.url, qualified=False)
        assert client.get("/api/diagnosis/status").status_code == 401
        viewer = sign_in(client, "viewer")
        assert client.get("/api/diagnosis/status").json()["quality"] == "not_established"
        route = "/api/diagnosis/checks"
        assert client.post(route, headers=viewer, json={}).status_code == 403
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        assert client.post(route, headers=headers, json={"endpoint": "evil"}).status_code == 422
        assert (
            client.post(route, headers=headers | {"X-CSRF-Token": "wrong"}, json={}).status_code
            == 403
        )
        created = client.post(route, headers=headers, json={})
        assert created.status_code == 201, created.text
        assert client.post(route, headers=headers, json={}).json()["id"] == created.json()["id"]
        assert (
            client.post(
                route, headers=headers | {"Idempotency-Key": str(uuid4())}, json={}
            ).status_code
            == 409
        )
        assert run_once()
        receipt = client.get(route).json()["items"][0]["receipt"]
        assert receipt["transport_success"] and receipt["schema_success"]
        assert receipt["usage"] is None
        assert receipt["quality"] == "not_established"
        status = client.get("/api/diagnosis/status").json()
        assert status["last_attempt_at"] and status["last_success_at"]
        assert status["last_error"] is None
        assert receipt["served_model"] == "fixture-served"
        assert receipt["response_sha256"] and receipt["finish_reason"] == "stop"
        assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("wire_status", "transport", "outcome"),
    [
        (200, "succeeded", "invalid_envelope"),
        (401, "rejected", "auth_denied"),
        (429, "rejected", "rate_limited"),
        (None, "unknown", "timeout"),
    ],
)
def test_model_connection_preserves_independent_evidence(
    client, deployment, wire_status, transport, outcome
):
    def handler(request):
        if wire_status is None:
            threading.Event().wait(0.7)
        else:
            respond(request, {"secret": "upstream-response-sentinel"}, status=wire_status)

    with serve(handler) as server:
        deployment(server.url, qualified=False, limits={"read_timeout_seconds": 0.2})
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/diagnosis/checks", headers=headers, json={}).status_code == 201
        assert not server.requests
        assert run_once()
        response = client.get("/api/diagnosis/status")
        assert response.status_code == 200
        result = response.json()
        assert result["last_attempt"]["transport"] == transport
        assert result["last_attempt"]["outcome"] == outcome
        assert result["last_attempt"]["usage"] is None
        assert result["last_attempt"]["observed_at"]
        assert bool(result["last_success_at"]) == (wire_status == 200)
        assert result["last_check"]["receipt"]["schema_success"] is False
        assert result["rca_validation"] is None
        assert result["quality"] == "not_established"
        assert "upstream-response-sentinel" not in response.text
        assert len(server.requests) == 1


def test_model_connection_keeps_zero_and_ages_or_clears_current_evidence(client, db, deployment):
    def handler(request):
        if len(request.server.requests) == 1:
            respond(request, text_payload("OK", usage={"total_tokens": 0}))
        else:
            respond(request, {}, status=429)

    with serve(handler) as server:
        deployment(server.url, qualified=False)
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/diagnosis/checks", headers=headers, json={}).status_code == 201
        assert run_once()
        status = client.get("/api/diagnosis/status").json()
        assert status["last_attempt"]["usage"] == {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": 0,
        }
        assert status["last_check"]["receipt"]["schema_success"] is True
        assert status["rca_validation"] is None
        assert status["limits"]["max_output_tokens"] == 32
        check = db.get(ModelCheck, UUID(status["last_check"]["id"]))
        # Age only the disposable fixture records so this test does not wait a day.
        offset = timedelta(hours=25)
        check.created_at -= offset
        check.admitted_at -= offset
        check.receipt = check.receipt | {
            "observed_at": (database_now(db) - offset).isoformat(),
            "raw_provider_body": "legacy-private-sentinel",
        }
        check.identity = check.identity | {"private_path": "/legacy-private-sentinel"}
        db.commit()
        history = client.get("/api/diagnosis/checks")
        assert "legacy-private-sentinel" not in history.text
        check.identity = load().identity()
        db.commit()
        assert client.get("/api/diagnosis/status").json()["last_attempt"]["stale"] is True
        retry = headers | {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/diagnosis/checks", headers=retry, json={}).status_code == 201
        assert run_once()
        latest = client.get("/api/diagnosis/status").json()
        assert latest["last_attempt"]["outcome"] == "rate_limited"
        assert latest["last_attempt"]["usage"] is None
        assert latest["last_success_at"]

        deployment(server.url, qualified=False, config_version="changed")
        result = client.get("/api/diagnosis/status").json()
        assert result["last_attempt"] is None and result["last_check"] is None
        assert result["last_success_at"] is None
        assert (
            client.get("/api/diagnosis/checks").json()["items"][1]["receipt"]["usage"][
                "total_tokens"
            ]
            == 0
        )
        assert len(server.requests) == 2


@pytest.mark.parametrize("offset", [-60, 60])
def test_current_probe_does_not_promote_an_impossible_receipt_time(client, db, deployment, offset):
    with serve(lambda h: respond(h, text_payload("OK"))) as server:
        deployment(server.url, qualified=False)
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        created = client.post("/api/diagnosis/checks", headers=headers, json={})
        assert created.status_code == 201
        assert run_once()
        check = db.get(ModelCheck, UUID(created.json()["id"]))
        previous = check.admitted_at - timedelta(minutes=10)
        db.add(
            ModelCheck(
                actor_id=check.actor_id,
                idempotency_key=uuid4(),
                identity=check.identity,
                dispatch_epoch=check.dispatch_epoch,
                state="completed",
                created_at=previous,
                admitted_at=previous,
                deadline=previous + timedelta(minutes=1),
                receipt=check.receipt | {"observed_at": previous.isoformat()},
            )
        )
        check.receipt = check.receipt | {
            "observed_at": (check.admitted_at + timedelta(seconds=offset)).isoformat()
        }
        db.commit()

        status = client.get("/api/diagnosis/status").json()
        assert status["last_attempt"]["transport"] == "unknown"
        assert status["last_attempt"]["observed_at"] is None
        assert datetime.fromisoformat(status["last_success_at"]) == previous
        assert status["last_check"]["receipt"] is None
        history = client.get("/api/diagnosis/checks").json()["items"][0]
        assert history["receipt"]["schema_success"] is True
        assert len(server.requests) == 1


def test_model_setup_and_unsent_check_remain_private_and_separate(client, deployment, monkeypatch):
    with serve(lambda h: pytest.fail("Missing worker credential must not send")) as server:
        deployment(
            server.url + "/private-root-canary",
            qualified=False,
            limits={"max_input_bytes": 32768},
        )
        monkeypatch.delenv("DIAGNOSIS_TEST_TOKEN")
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/diagnosis/checks", headers=headers, json={}).status_code == 201
        assert run_once()

        response = client.get("/api/diagnosis/status")
        assert "private-root-canary" not in response.text
        assert "DIAGNOSIS_TEST_TOKEN" not in response.text
        result = response.json()
        assert result["last_attempt"] is None
        assert result["last_success_at"] is None
        assert result["last_check"]["admitted_at"] is None
        assert result["last_check"]["receipt"]["kind"] == "credential_unavailable"
        assert result["limits"]["max_request_bytes"] == 32768
        assert result["limits"]["max_message_bytes"] == 65536
        assert result["limits"]["max_context_bytes"] == 8192
        assert not server.requests


def test_probe_metadata_excludes_credential_and_error_body(client, deployment):
    token = "owned-loopback-test-token"
    with serve(lambda h: respond(h, text_payload("OK") | {"model": token})) as server:
        deployment(server.url, qualified=False)
        headers = sign_in(client) | {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/diagnosis/checks", headers=headers, json={}).status_code == 201
        assert run_once()
        response = client.get("/api/diagnosis/checks")
        assert token not in response.text
        assert response.json()["items"][0]["receipt"]["served_model"] is None


def test_legacy_no_backfill_and_operational_read_excludes_evaluation(db, context, deployment):
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url, mode="evaluation_only", qualified=False)
        run = db.get(InvestigationRun, context[1])
        original = (run.state, run.reason, run.id)
        assert analysis_view(db, run.id).status == "not_available"
        evaluation = admit(db, run, purpose="evaluation")
        db.commit()
        assert evaluation is not None
        assert analysis_view(db, run.id).analysis_id is None
        assert (run.state, run.reason, run.id) == original
        assert not server.requests


def test_evaluation_uses_real_pipeline_but_public_selection_cannot_adopt_it(
    client, db, context, deployment, tmp_path
):
    from knowledge_fixtures import document, write_corpus
    from cloudops.features.knowledge.indexing import index_corpus
    from cloudops.features.knowledge.models import KnowledgeSelection, KnowledgeSelectionChunk

    write_corpus(
        tmp_path,
        [document(title="CrashLoopBackOff guide")],
        {
            "fixture-guide": "# CrashLoopBackOff guide\n\nCrashLoopBackOff workload startup evidence.\n"
        },
    )
    assert index_corpus(tmp_path).outcome == "indexed"
    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url, mode="evaluation_only", qualified=False)
        run = db.get(InvestigationRun, context[1])
        original = run.state, run.reason, run.finished_at
        analysis = admit(db, run, purpose="evaluation")
        db.commit()
        assert run_once()
        assert refresh(db, analysis.id).state == "completed"
        assert db.get(AnalysisResult, analysis.id) is not None
        db.refresh(run)
        assert (run.state, run.reason, run.finished_at) == original
        selection = db.scalar(
            select(KnowledgeSelection).where(
                KnowledgeSelection.request_identity == str(analysis.id)
            )
        )
        member = db.scalar(
            select(KnowledgeSelectionChunk).where(
                KnowledgeSelectionChunk.selection_id == selection.id
            )
        )
        assert member is not None
        sign_in(client)
        response = client.get(f"/api/incidents/{context[0]}/runs/{context[1]}/analysis")
        assert response.json()["result"] is None
        status = client.get("/api/diagnosis/status").json()
        assert status["quality"] == "not_established"
        assert status["rca_validation"]["outcome"] == "validated"
        assert status["rca_validation"]["purpose"] == "evaluation"
        assert status["last_attempt"]["source"] == "evaluation"
        assert status["last_attempt"]["usage"]["total_tokens"] == 12
        chunk = next(
            item
            for item in selection.payload["chunks"]
            if item["reference"]["chunk_id"] == str(member.chunk_id)
        )
        route = f"/api/incidents/{context[0]}/runs/{context[1]}/knowledge/selections/{selection.id}/chunks/{member.chunk_id}"
        assert (
            client.get(
                route, params={"document_version_id": chunk["reference"]["document_version_id"]}
            ).status_code
            == 404
        )
        assert len(server.requests) == 1


def test_qualification_mode_change_preserves_behavior(deployment):
    with serve(lambda h: respond(h, text_payload("OK"))) as server:
        model, egress = deployment(server.url)
        promoted = model.model_copy(update={"mode": "evaluation_only"})
        assert promoted.behavior_digest() == model.behavior_digest()
        assert load().denied() is None
        changed = model.model_copy(update={"model": "different"})
        assert Deployment(changed, egress, load().qualification).denied() == "unqualified"


@pytest.mark.parametrize("kind", ["invalid", "refused"])
def test_invalid_and_refused_never_promote_findings(db, context, deployment, kind):
    _crashloop(db, context)
    payload = text_payload("invalid JSON") if kind == "invalid" else refusal_payload()
    with serve(lambda h: respond(h, payload)) as server:
        deployment(server.url)
        identifier = admission(db, context)
        run_once()
        if kind == "invalid":
            run_once()
        analysis = refresh(db, identifier)
        assert analysis.state == "failed"
        assert analysis.calls == (2 if kind == "invalid" else 1)
        assert analysis.repairs <= 1
        assert db.get(AnalysisResult, identifier) is None
        capture(db, context, kind)


def test_valid_model_abstention_is_insufficient_not_completed(db, context, deployment):
    _crashloop(db, context)
    payload = text_payload(
        _raw(
            status="insufficient_evidence",
            cause_category="unknown",
            candidate_template_id=None,
            missing_evidence=["Causality is unproven."],
        )
    )
    with serve(lambda h: respond(h, payload)) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        analysis = refresh(db, identifier)
        assert (
            analysis.state == "insufficient_evidence" and analysis.reason == "validated_abstention"
        )
        run = db.get(InvestigationRun, context[1])
        assert run.state == "insufficient_evidence" and run.provider_call is None
        result = db.get(AnalysisResult, identifier).payload
        assert (
            result["status"] == "insufficient_evidence" and result["candidate_template_id"] is None
        )
        receipt = db.scalar(
            select(AnalysisCall).where(AnalysisCall.analysis_id == identifier)
        ).receipt
        assert receipt["response_text_sha256"] and receipt["finish_reason"] == "stop"
        assert receipt["served_model"] == "fixture-served"
        capture(db, context, "model-abstention")


@pytest.mark.parametrize("wire_status, expected", [(401, "auth_denied"), (500, "upstream_error")])
def test_failure_classification_and_due_without_recollection(
    db, context, deployment, wire_status, expected
):
    _crashloop(db, context)
    with serve(lambda h: respond(h, {}, status=wire_status)) as server:
        deployment(server.url)
        identifier = admission(db, context)
        run_once()
        analysis = refresh(db, identifier)
        assert analysis.reason == expected
        if wire_status == 500:
            assert analysis.wait_mode == "automatic_retry"
            assert not run_once()
            for number in (2, 3):
                db.expire_all()
                db.scalar(select(Job).where(Job.subject_id == identifier)).due_at = database_now(
                    db
                ) - timedelta(seconds=1)
                db.commit()
                assert run_once()
                assert refresh(db, identifier).calls == number
            assert analysis.state == "failed"
            assert len(server.requests) == 3
        else:
            assert analysis.state == "failed" and len(server.requests) == 1


def test_cancel_wins_after_context_before_send(client, db, context, deployment):
    _crashloop(db, context)
    with serve(lambda h: pytest.fail("Cancelled work must not send")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with ModelSlot() as slot:
            claim = claim_next({ANALYSIS_JOB: HANDLERS[ANALYSIS_JOB]}, GATE)
            snapshot, _, checked = _prepare(claim, GATE, slot)
            response = client.post(
                f"/api/incidents/{context[0]}/runs/{context[1]}/cancel",
                json={},
                headers=sign_in(client),
            )
            assert response.status_code == 200, response.text
            with pytest.raises(Interrupted):
                _reserve(claim, GATE, slot, checked, threading.Event(), snapshot)
        assert refresh(db, identifier).state == "cancelled"
        assert db.get(AnalysisContext, identifier).snapshot is None
        assert db.scalar(select(func.count()).select_from(EvidencePin)) == 0
        assert not server.requests


def test_cancel_and_reservation_contend_on_run_without_lock_inversion(db, context, deployment):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from cloudops.features.investigations.service import terminalize
    from cloudops.shared.database import sessions

    _crashloop(db, context)
    with serve(lambda h: pytest.fail("Cancellation won the send boundary")) as server:
        deployment(server.url)
        identifier = admission(db, context)
        with ModelSlot() as slot, ThreadPoolExecutor(max_workers=1) as executor:
            claim = claim_next({ANALYSIS_JOB: HANDLERS[ANALYSIS_JOB]}, GATE)
            snapshot, _, checked = _prepare(claim, GATE, slot)
            with sessions()() as cancelling:
                run = cancelling.scalar(
                    select(InvestigationRun)
                    .where(InvestigationRun.id == context[1])
                    .with_for_update()
                )
                locker = cancelling.scalar(text("SELECT pg_backend_pid()"))
                future = executor.submit(
                    _reserve, claim, GATE, slot, checked, threading.Event(), snapshot
                )
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with sessions()() as observer:
                        blocked = observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :locker = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"locker": locker},
                        )
                    if blocked:
                        break
                    threading.Event().wait(0.02)
                assert blocked, "Reservation did not contend on the held Run row."
                terminalize(cancelling, run, "operator_cancelled")
                cancelling.commit()
                with pytest.raises(Interrupted):
                    future.result(timeout=5)
        assert refresh(db, identifier).state == "cancelled"
        assert db.scalar(select(func.count()).select_from(AnalysisCall)) == 0
        assert not server.requests


def test_in_flight_cancel_conflict_and_resolve_preserves_historical_advice(
    client, db, context, deployment
):
    from concurrent.futures import ThreadPoolExecutor
    from cloudops.features.incidents.models import Incident
    from cloudops.features.incidents.service import cancel_pending
    from cloudops.shared.database import sessions

    _crashloop(db, context)
    entered, release = threading.Event(), threading.Event()

    def handler(h):
        entered.set()
        assert release.wait(20)
        respond(h, text_payload(_raw()))

    with serve(handler) as server, ThreadPoolExecutor(max_workers=1) as executor:
        deployment(server.url)
        identifier = admission(db, context)
        headers = sign_in(client)
        future = executor.submit(run_once)
        try:
            assert entered.wait(10)
            response = client.post(
                f"/api/incidents/{context[0]}/runs/{context[1]}/cancel", json={}, headers=headers
            )
            assert response.status_code == 409
            with sessions()() as tx, tx.begin():
                incident = tx.scalar(
                    select(Incident).where(Incident.id == context[0]).with_for_update()
                )
                incident.state = "resolved"
                cancel_pending(tx, incident.id, "incident_resolved")
        finally:
            release.set()
        assert future.result(timeout=15)
        assert refresh(db, identifier).state == "completed"
        assert db.get(Incident, context[0]).state == "resolved"
        assert len(server.requests) == 1


def test_guard_connection_loss_during_wire_rejects_result(db, context, deployment):
    import time
    from cloudops.shared.database import sessions

    _crashloop(db, context)

    def handler(h):
        with sessions()() as tx, tx.begin():
            assert tx.scalar(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks "
                    "WHERE locktype='advisory' AND objid=742070 AND granted "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) LIMIT 1"
                )
            )
        respond(h, text_payload(_raw()))

    with serve(handler) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        assert db.get(AnalysisResult, identifier) is None
        deadline = time.monotonic() + 5
        while True:
            with sessions()() as observed:
                held = observed.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE "
                        "locktype='advisory' AND objid=742070 AND granted AND "
                        "database=(SELECT oid FROM pg_database WHERE datname=current_database()))"
                    )
                )
            if not held:
                break
            assert time.monotonic() < deadline, (
                "Terminated session did not release its model guard."
            )
            threading.Event().wait(0.02)
        db.expire_all()
        db.scalar(select(Job).where(Job.subject_id == identifier)).lease_until = database_now(
            db
        ) - timedelta(seconds=1)
        db.commit()
        run_once()
        assert refresh(db, identifier).reason == "provider_outcome_unknown"
        assert db.scalar(select(func.count()).select_from(AnalysisCall)) == 1
        assert len(server.requests) == 1


def test_atomic_collection_handoff_rolls_back_and_preserves_saved_source(db, context, deployment):
    from cloudops.features.investigations.collector import CollectionHandler, FinalizeCollection
    from cloudops.features.investigations.models import Evidence
    from cloudops.features.investigations.states import COLLECTION_JOB
    from cloudops.shared.jobs import finish

    _crashloop(db, context)
    with serve(lambda h: respond(h, text_payload(_raw()))) as server:
        deployment(server.url)
        run = db.get(InvestigationRun, context[1])
        run.state, run.finished_at, run.pack_coverage = "queued", None, None
        db.add(Job(kind=COLLECTION_JOB, subject_id=run.id))
        db.commit()
        original = [
            (e.id, e.sha256, e.payload) for e in db.scalars(select(Evidence).order_by(Evidence.id))
        ]
        db.commit()
        handler = CollectionHandler(finalize=admit)
        claim = claim_next({COLLECTION_JOB: handler}, GATE)
        from cloudops.features.investigations.catalog import SOURCES

        configured = {
            "kubernetes.state",
            "kubernetes.events",
            "kubernetes.logs.current",
            "kubernetes.logs.previous",
        }
        result = FinalizeCollection(context[2], tuple(set(SOURCES) - configured))

        def broken(tx, saved_run):
            admit(tx, saved_run)
            raise RuntimeError("Owned atomicity fault")

        with pytest.raises(RuntimeError, match="atomicity fault"):
            finish(claim, CollectionHandler(finalize=broken), result=result, gate=GATE)
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(Analysis)) == 0
        db.commit()
        assert finish(claim, handler, result=result, gate=GATE)
        db.expire_all()
        analysis = db.scalar(select(Analysis))
        assert analysis is not None
        assert [
            (e.id, e.sha256, e.payload)
            for e in db.scalars(
                select(Evidence)
                .where(Evidence.id.in_([row[0] for row in original]))
                .order_by(Evidence.id)
            )
        ] == original
        db.commit()
        assert run_once()
        assert refresh(db, analysis.id).state == "completed"
        assert len(server.requests) == 1


def test_call_longer_than_the_job_lease_still_publishes_after_renewal(
    db, context, deployment, monkeypatch
):
    """A reasoning-length call outlives one lease period; the worker renews it throughout."""
    from dataclasses import replace

    import cloudops.entrypoints.worker as worker_module
    import cloudops.shared.jobs as jobs_module
    from cloudops.shared.config import settings as real_settings

    short = replace(real_settings(), lease_seconds=1, heartbeat_seconds=0.2)
    monkeypatch.setattr(jobs_module, "settings", lambda: short)
    monkeypatch.setattr(worker_module, "settings", lambda: short)
    _crashloop(db, context)

    def slow(handler):
        threading.Event().wait(2.5)
        respond(handler, text_payload(_raw()))

    with serve(slow) as server:
        deployment(server.url, limits={"deadline_seconds": 10, "read_timeout_seconds": 10})
        identifier = admission(db, context)
        assert run_once()
        analysis = refresh(db, identifier)
        assert analysis.state == "completed", analysis.reason
        assert analysis.calls == 1 and len(server.requests) == 1
        assert db.get(AnalysisResult, identifier).payload["status"] == "supported"
