"""Trusted finite E06 campaign. Never collected by pytest or invoked by CI."""

import argparse
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, admit, terminal
from cloudops.features.diagnosis.models import Analysis, AnalysisResult
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.service import analysis_view, enqueue_check
from cloudops.features.diagnosis.settings import Deployment, load
from cloudops.features.diagnosis.transport import ModelClient
from cloudops.features.diagnosis.schemas import parse_usage
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.investigations.comparison_service import citation
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job, retire_queued

FIXTURES = Path(__file__).parent / "fixtures/e06"
MODELS = ("gpt-6.1-sol", "codex/gpt-6.1-terra")
OWNED_DATABASE = "cloudops_e06_gpt_test"
MAX_REQUESTS = 50
TOKEN_STOP = 60000


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


class Budget:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text())

    def check(self, model, repair):
        if model not in MODELS:
            raise ValueError("Unapproved model")
        if self.data.get("prior_requests", 0) + len(self.data["calls"]) >= MAX_REQUESTS:
            raise ValueError("Request budget exhausted")
        if self.data["observed_tokens"] >= TOKEN_STOP:
            raise ValueError("Observed token stop reached")
        if repair and self.data["repairs"] >= 1:
            raise ValueError("Shared repair reserve exhausted")
        count = sum(call["model"] == model for call in self.data["calls"])
        if count >= (14 if model == MODELS[0] else 1):
            raise ValueError("Per-model request budget exhausted")

    def reserve(self, model, case, repair):
        self.check(model, repair)
        self.data["calls"].append(
            {"model": model, "case": case, "repair": repair, "outcome": "unsettled"}
        )
        self.data["repairs"] += int(repair)
        self.data["total_requests"] = self.data.get("prior_requests", 0) + len(self.data["calls"])
        save(self.path, self.data)

    def settle(self, call, secret):
        record = asdict(call)
        if call.text:
            try:
                value = json.loads(call.text)
                record["text"] = json.dumps(value, ensure_ascii=False)
            except ValueError:
                record["text"] = call.text if call.text.strip() == "OK" else None
                record["text_omission"] = (
                    "Non-JSON output retained by bounded outcome and response digest."
                )

        def redact(value):
            if isinstance(value, str):
                return value.replace(secret, "[credential removed]") if secret else value
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        record = redact(record)
        self.data["calls"][-1]["outcome"] = record
        if call.usage:
            self.data["observed_tokens"] += max(
                call.usage.total_tokens or 0,
                (call.usage.prompt_tokens or 0) + (call.usage.completion_tokens or 0),
            )
        if call.usage is None or call.usage.total_tokens is None:
            self.data["missing_usage_calls"] += 1
        save(self.path, self.data)


def configure(root, model, *, operational=False):
    primary = model == MODELS[0]
    profile = ModelProfile.model_validate(
        {
            "schema": "DiagnosisModelProfile/v1",
            "config_id": "e06-primary" if primary else "e06-secondary",
            "config_version": "2",
            "mode": "operational" if operational else "evaluation_only",
            "endpoint_label": "owned-local-gateway",
            "base_url": "http://localhost:20128/v1",
            "allow_insecure_loopback": True,
            "model": model,
            "credential": {"kind": "env", "name": "DIAGNOSIS_LIVE_TOKEN"},
            "output_limit": {"param": "max_completion_tokens", "value": 1024 if primary else 64},
            "limits": {
                "read_timeout_seconds": 30,
                "deadline_seconds": 30,
                "max_input_bytes": 32768,
            },
            "authorization_id": "owner-authorized-e06-local-gateway",
            "budget_policy_id": "e06-finite-50-requests",
        }
    )
    policy = EgressPolicy.model_validate(
        {
            "schema": "EgressPolicy/v1",
            "policy_id": "e06-owned-controlled-fixtures",
            "policy_version": "1",
            "destination": {
                "config_id": profile.config_id,
                "config_version": profile.config_version,
                "endpoint_label": profile.endpoint_label,
            },
            "allowed_source_classes": [
                "operator_request",
                "kubernetes_state",
                "kubernetes_events",
                "container_logs",
                "prometheus_series",
                "diagnostic_fixture",
            ],
            "max_message_bytes": 32768,
            "max_target_bytes": 4096,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": [],
        }
    )
    for name, value in [
        ("profile", profile.model_dump(mode="json", by_alias=True)),
        ("egress", policy.model_dump(mode="json", by_alias=True)),
    ]:
        path = root / (name + ".json")
        save(path, value)
        os.environ["DIAGNOSIS_" + name.upper() + "_FILE"] = str(path)
    if operational:
        os.environ["DIAGNOSIS_QUALIFICATION_FILE"] = str(root / "qualification.json")
        if load().denied():
            raise ValueError("Exact primary qualification required before operational smoke")
    else:
        os.environ.pop("DIAGNOSIS_QUALIFICATION_FILE", None)
    return Deployment(profile, policy, None).identity()


def seed_case(db, actor, case):
    from test_diagnosis_validation import add_evidence

    workload = db.scalar(select(Workload).where(Workload.active.is_(True)).order_by(Workload.id))
    incident = Incident(
        workload_id=workload.id,
        title="Owned E06 acceptance investigation",
        description=case["source_label"],
        created_by=actor,
    )
    db.add(incident)
    db.flush()
    now = database_now(db)
    run = InvestigationRun(
        incident_id=incident.id,
        requested_by=actor,
        state="insufficient_evidence",
        reason="Controlled acceptance input",
        question=case["question"],
        question_source="operator",
        window_start=now - timedelta(minutes=30),
        window_end=now,
        window_source="explicit",
    )
    db.add(run)
    db.commit()
    scope = incident.id, run.id, workload.uid
    for key, original in case["evidence"].items():
        payload = json.loads(json.dumps(original))
        if "series" in payload:
            for series in payload["series"]:
                series["samples"] = [
                    [int(now.timestamp()) - 120 + 60 * i, value]
                    for i, (_, value) in enumerate(series["samples"])
                ]
        if "events" in payload:
            for event in payload["events"]:
                event["lastTimestamp"] = now.isoformat()
        row = add_evidence(db, scope, key, payload)
        row.sha256 = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        db.commit()
    return run.id


def case_result(db, analysis_id):
    db.expire_all()
    analysis = db.get(Analysis, analysis_id)
    stored = db.get(AnalysisResult, analysis_id)
    resolved = []
    if stored:
        for reference in stored.payload["supporting"]:
            ref = reference["citation"]
            from uuid import UUID

            target = citation(
                db,
                UUID(stored.payload["incident_id"]),
                analysis.run_id,
                UUID(ref["evidence_id"]),
                ref["locator"]["kind"],
                ref["locator"]["key"],
            )
            resolved.append(target.model_dump(mode="json"))
    return {
        "analysis_id": str(analysis_id),
        "run_id": str(analysis.run_id),
        "purpose": analysis.purpose,
        "state": analysis.state,
        "reason": analysis.reason,
        "identity": analysis.identity,
        "calls": analysis.calls,
        "repairs": analysis.repairs,
        "result": stored.payload if stored else None,
        "resolved_support": resolved,
        "excluded_from_operational_api": analysis_view(db, analysis.run_id).result is None
        if analysis.purpose == "evaluation"
        else None,
    }


def execute_case(db, actor, case, budget, current, *, operational=False):
    from cloudops.entrypoints.worker import run_once

    run_id = seed_case(db, actor, case)
    analysis = admit(
        db, db.get(InvestigationRun, run_id), purpose="operational" if operational else "evaluation"
    )
    if analysis is None:
        raise ValueError("Analysis admission denied")
    identifier = analysis.id
    db.commit()
    current.update(case=case["id"], repair=False)
    if not run_once():
        raise ValueError("Owned worker could not claim admitted case")
    db.expire_all()
    analysis = db.get(Analysis, identifier)
    if analysis.state == "pending" and analysis.phase == "repair" and budget.data["repairs"] == 0:
        current["repair"] = True
        if not run_once():
            raise ValueError("Selected repair not claimable")
        db.expire_all()
        analysis = db.get(Analysis, identifier)
    if analysis.state in {"pending", "analyzing", "waiting_for_quota"}:
        terminal(
            db,
            analysis,
            db.get(InvestigationRun, run_id),
            "failed",
            "acceptance_no_additional_call",
        )
        retire_queued(db, ANALYSIS_JOB, identifier, "Finite campaign stops additional calls")
        job = db.scalar(select(Job).where(Job.kind == ANALYSIS_JOB, Job.subject_id == identifier))
        if job.state == "held":
            job.state = "cancelled"
        db.commit()
    result = case_result(db, identifier)
    budget.data["cases"][case["id"]] = result
    save(budget.path, budget.data)
    print(
        json.dumps(
            {
                "case": case["id"],
                "state": result["state"],
                "reason": result["reason"],
                "requests": budget.data.get("prior_requests", 0) + len(budget.data["calls"]),
                "observed_tokens": budget.data["observed_tokens"],
            }
        ),
        flush=True,
    )
    return result


def scope_negative(db, budget):
    from fastapi.testclient import TestClient
    from cloudops.entrypoints.api import app
    from conftest import sign_in
    from uuid import UUID

    run_id = UUID(budget.data["cases"]["n-required-gap"]["run_id"])
    incident_id = db.get(InvestigationRun, run_id).incident_id
    before = len(budget.data["calls"])
    with TestClient(app) as client:
        headers = sign_in(client)
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post(
            f"/api/incidents/{incident_id}/runs",
            headers=headers,
            json={
                "workload_id": str(uuid4()),
                "reason": "Inspect another unapproved workload",
                "window": {"preset": 30},
            },
        )
    if response.status_code != 409 or response.json()["error"]["code"] != "target_mismatch":
        raise ValueError("Foreign workload request was not rejected at the target boundary")
    assert len(budget.data["calls"]) == before
    budget.data["scope_negative"] = {
        "status": response.status_code,
        "code": "target_mismatch",
        "http_model_calls": 0,
    }
    save(budget.path, budget.data)


def carried_probe(path):
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Prior probe receipt exceeds bound")
    evidence = json.loads(raw)
    if evidence.get("endpoint") != "http://localhost:20128/v1" or evidence.get("output_limit") != {
        "param": "max_completion_tokens",
        "value": 64,
    }:
        raise ValueError("Prior probe destination or output contract changed")
    count = evidence.get("prior_model_requests")
    if type(count) is not int or not 0 <= count < MAX_REQUESTS:
        raise ValueError("Invalid prior request count")
    unknown, observed, secondary = count, 0, None
    for row in evidence["results"]:
        if not row["admitted"]:
            continue
        count += 1
        outcome = row["outcome"]
        if not isinstance(outcome, dict):
            raise ValueError("Prior call is unsettled")
        usage, _ = parse_usage(outcome.get("usage"))
        if usage:
            observed += max(
                usage.total_tokens or 0, (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
            )
        unknown += int(usage is None or usage.total_tokens is None)
        if row["configured_model"] == MODELS[1]:
            if not (
                row["schema_success"]
                and outcome.get("kind") == "text"
                and outcome.get("http_status") == 200
                and outcome.get("text", "").strip() == "OK"
                and outcome.get("identity", {}).get("requested_model") == MODELS[1]
            ):
                raise ValueError("Prior secondary probe did not pass its exact contract")
            secondary = {
                "identity": row["profile_identity"],
                "policy_digest": row["policy_digest"],
                "state": "completed",
                "receipt": {
                    key: outcome.get(key)
                    for key in [
                        "kind",
                        "served_model",
                        "finish_reason",
                        "usage",
                        "usage_status",
                        "elapsed_ms",
                        "response_sha256",
                    ]
                },
                "quality": "not_established",
                "source": "Reused owner-authorized fixed probe; no repeat HTTP request",
            }
            secondary["receipt"].update(transport_success=True, schema_success=True)
    if count != evidence.get("model_requests_total") or count >= MAX_REQUESTS or secondary is None:
        raise ValueError("Prior request history or secondary proof is incomplete")
    return {
        "prior_requests": count,
        "total_requests": count,
        "observed_tokens": observed,
        "missing_usage_calls": unknown,
        "secondary_probe": secondary,
        "carryover_sha256": hashlib.sha256(raw).hexdigest(),
    }


def freeze(root, carryover=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (root / "ledger.json").exists():
        raise ValueError("Campaign already exists; never overwrite an initial cohort")
    plan = {
        "cohort": "e06-gpt-owned-fixtures-v1",
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "driver_sha256": sha(Path(__file__)),
        "cases_sha256": sha(FIXTURES / "cases.json"),
        "oracle_sha256": sha(FIXTURES / "oracle.json"),
        "models": MODELS,
        "endpoint": "http://localhost:20128/v1",
        "request_limit": MAX_REQUESTS,
        "observed_token_stop": TOKEN_STOP,
        "allowance": "Owner authorized 50 requests across all attempts; this cohort uses at most 14 primary calls, one shared repair, and reuses a passing secondary fixed probe when supplied; no implicit retry",
        "source_authority": "Owner-authorized synthetic owned-lab fixtures only; no private real telemetry",
        "calls": [],
        "cases": {},
        "repairs": 0,
        "observed_tokens": 0,
        "missing_usage_calls": 0,
    }
    if carryover is not None:
        plan.update(carried_probe(carryover))
    save(root / "ledger.json", plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["freeze", "run", "smoke"])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--carryover-probe", type=Path)
    args = parser.parse_args()
    root = args.state_dir.resolve()
    if args.phase == "freeze":
        freeze(root, args.carryover_probe)
        return
    if (
        make_url(os.environ.get("DATABASE_URL", "")).database != OWNED_DATABASE
        or os.environ.get("E06_OWNED_LAB") != "e06-r5"
    ):
        raise ValueError("Only the explicitly owned cloudops_e06_gpt_test lab is allowed")
    if not os.environ.get("DIAGNOSIS_LIVE_TOKEN"):
        raise ValueError("Worker credential reference is unavailable")
    lock = root / "campaign.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        budget = Budget(root / "ledger.json")
        if any(
            budget.data[key] != sha(path)
            for key, path in [
                ("driver_sha256", Path(__file__)),
                ("cases_sha256", FIXTURES / "cases.json"),
                ("oracle_sha256", FIXTURES / "oracle.json"),
            ]
        ):
            raise ValueError("Frozen campaign source changed")
        if args.phase == "run" and (budget.data["calls"] or budget.data.get("started")):
            raise ValueError("Initial campaign already started; no repeat cohort is authorized")
        if args.phase == "smoke" and (
            not budget.data.get("evaluation_finished")
            or budget.data.get("smoke_started")
            or any(call["case"] == "operational-smoke" for call in budget.data["calls"])
            or "operational-smoke" in budget.data["cases"]
        ):
            raise ValueError("Smoke requires completed initial evaluation and cannot repeat")
        current = {"case": "", "repair": False}
        original = ModelClient.send

        def counted(client, messages, *, before_send):
            def reserve():
                budget.check(client.profile.model, current["repair"])
                before_send()
                budget.reserve(client.profile.model, current["case"], current["repair"])

            outcome = original(client, messages, before_send=reserve)
            if budget.data["calls"] and budget.data["calls"][-1]["outcome"] == "unsettled":
                budget.settle(outcome, os.environ["DIAGNOSIS_LIVE_TOKEN"])
            return outcome

        ModelClient.send = counted
        try:
            from cloudops.features.access.models import User

            with sessions()() as db:
                actor = db.scalar(select(User.id).where(User.username == "operator"))
                if not actor:
                    raise ValueError("Provision the disposable lab operator first")
                if db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.state.in_(["queued", "running", "held"]))
                ):
                    raise ValueError("Dedicated campaign lab has other active work")
                cases = json.loads((FIXTURES / "cases.json").read_text())
                identity = configure(root, MODELS[0], operational=args.phase == "smoke")
                if args.phase == "smoke":
                    budget.data["smoke_started"] = True
                    save(budget.path, budget.data)
                    case = dict(cases[0], id="operational-smoke")
                    execute_case(db, actor, case, budget, current, operational=True)
                    return
                budget.data.update(started=True, primary_identity=identity)
                save(budget.path, budget.data)
                execute_case(
                    db,
                    actor,
                    {
                        "id": "n-required-gap",
                        "question": "What does this evidence support?",
                        "source_label": "No telemetry captured; controlled negative.",
                        "evidence": {},
                    },
                    budget,
                    current,
                )
                if budget.data["calls"]:
                    raise ValueError("Required-gap case unexpectedly used network")
                scope_negative(db, budget)
                for case in cases:
                    result = execute_case(db, actor, case, budget, current)
                    last = budget.data["calls"][-1]["outcome"] if budget.data["calls"] else {}
                    if isinstance(last, dict) and last.get("kind") not in {"text", "truncated"}:
                        raise ValueError(
                            "Transport/configuration failure: stop instead of repeating across cases"
                        )
                    if (
                        result["state"] not in {"completed", "insufficient_evidence"}
                        and len(budget.data["cases"]) <= 2
                    ):
                        raise ValueError(
                            "Initial schema failure persists: stop rather than spend remaining cohort"
                        )
                if "secondary_probe" not in budget.data:
                    configure(root, MODELS[1])
                    db.rollback()
                    current.update(case="secondary-compatibility", repair=False)
                    check = enqueue_check(db, actor, uuid4())
                    from cloudops.entrypoints.worker import run_once

                    assert run_once()
                    from cloudops.features.diagnosis.models import ModelCheck

                    db.expire_all()
                    checked = db.get(ModelCheck, check.id)
                    budget.data["secondary_probe"] = {
                        "identity": checked.identity,
                        "state": checked.state,
                        "receipt": checked.receipt,
                        "quality": "not_established",
                    }
                budget.data["evaluation_finished"] = True
                save(budget.path, budget.data)
        finally:
            ModelClient.send = original
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
