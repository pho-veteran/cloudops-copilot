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
OWNED_LAB = "e06-r5"
MAX_REQUESTS = 50
TOKEN_STOP = 170000
PRIMARY_LIMIT = 14
MAX_REPAIRS = 1
# Set only by a trusted campaign config (E06CampaignConfig/v1); None keeps the
# original Sol/Terra local-gateway campaign unchanged.
CONFIG = None
CONFIG_SCHEMA = "E06CampaignConfig/v1"
_DEFAULTS = {
    "CONFIG": None,
    "MODELS": MODELS,
    "OWNED_DATABASE": OWNED_DATABASE,
    "OWNED_LAB": OWNED_LAB,
    "MAX_REQUESTS": MAX_REQUESTS,
    "TOKEN_STOP": TOKEN_STOP,
    "PRIMARY_LIMIT": PRIMARY_LIMIT,
    "MAX_REPAIRS": MAX_REPAIRS,
}


def reset_config():
    """Each invocation starts from the original campaign; a config is never inherited."""
    globals().update(_DEFAULTS)


def _profile_snapshot(path):
    """Parse and hash one byte snapshot so later reads cannot swap the profile."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Campaign profile exceeds bound")
    value = json.loads(raw.decode("utf-8"))
    ca_file = (value.get("tls") or {}).get("ca_file")
    if ca_file and not Path(ca_file).is_absolute():
        # Installed copies live in the state directory; keep the trusted CA location.
        value["tls"]["ca_file"] = str((path.parent / ca_file).resolve())
    profile = ModelProfile.model_validate(value)
    profile._base_dir = path.parent
    if profile.tls.ca_file is not None:
        profile.ssl_context()
    return profile, hashlib.sha256(raw).hexdigest()


def load_config(path):
    """Read and validate a trusted, secret-free campaign config; apply its bounds."""
    global CONFIG, MODELS, OWNED_DATABASE, OWNED_LAB, MAX_REQUESTS, TOKEN_STOP
    global PRIMARY_LIMIT, MAX_REPAIRS
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Campaign config exceeds bound")
    value = json.loads(raw)
    required = {
        "schema",
        "cohort",
        "profile",
        "authorization_id",
        "budget_policy_id",
        "owned_database",
        "owned_lab",
        "max_requests",
        "max_repairs",
        "observed_token_stop",
        "max_reserved_usd",
        "input_usd_per_mtok",
        "output_usd_per_mtok",
        "rates_date",
    }
    optional = {"prior_receipts", "capture_contexts"}
    if value.get("schema") != CONFIG_SCHEMA or set(value) - required - optional:
        raise ValueError("Unsupported campaign config")
    if required - set(value):
        raise ValueError("Campaign config is incomplete")
    if type(value.get("capture_contexts", False)) is not bool:
        raise ValueError("capture_contexts must be a boolean")
    if not str(value["owned_database"]).endswith("_test"):
        raise ValueError("The owned campaign database must end in _test")
    for key in ("max_requests", "max_repairs", "observed_token_stop"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    for key in ("max_reserved_usd", "input_usd_per_mtok", "output_usd_per_mtok"):
        if not isinstance(value[key], (int, float)) or not 0 <= value[key] < 1000:
            raise ValueError(f"{key} must be a bounded non-negative number")
    profile_path = Path(value["profile"])
    if not profile_path.is_absolute():
        profile_path = path.parent / profile_path
    profile, profile_sha256 = _profile_snapshot(profile_path)
    if not profile.model or profile.output_limit is None or profile.credential is None:
        raise ValueError("The campaign profile must name a model, output limit and credential")
    if profile.credential.name != "DIAGNOSIS_LIVE_TOKEN":
        raise ValueError("The campaign worker credential must be DIAGNOSIS_LIVE_TOKEN")
    prior = []
    for item in value.get("prior_receipts", []):
        receipt = Path(item) if Path(item).is_absolute() else path.parent / item
        prior.append({"path": receipt.name, "sha256": sha(receipt)})
    CONFIG = {
        **value,
        "profile_path": str(profile_path),
        "profile_sha256": profile_sha256,
        "profile": profile,
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "prior_receipt_hashes": prior,
        "model": profile.model,
        "base_url": profile.base_url,
        "per_request_reserve_usd": (
            profile.limits.max_input_bytes * value["input_usd_per_mtok"]
            + profile.output_limit.value * value["output_usd_per_mtok"]
        )
        / 1_000_000,
    }
    MODELS = (profile.model,)
    OWNED_DATABASE, OWNED_LAB = value["owned_database"], value["owned_lab"]
    MAX_REQUESTS = PRIMARY_LIMIT = value["max_requests"]
    MAX_REPAIRS, TOKEN_STOP = value["max_repairs"], value["observed_token_stop"]
    return CONFIG


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def capture_context(root, case_id, prepared):
    """Keep the exact prepared context a send used; the product discards it at terminal state.

    Opt-in (``capture_contexts`` in a campaign config) so a later evaluation can bind
    an immutable bundle to the product result. A repair reuses the stored snapshot, so
    a second capture for the same case must be byte-identical.
    """
    directory = root / "contexts"
    directory.mkdir(mode=0o700, exist_ok=True)
    raw = (prepared.model_dump_json() + "\n").encode("utf-8")
    path = directory / f"{case_id}.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # The repair send re-reads the snapshot from JSONB, which may reorder keys.
        if json.loads(path.read_bytes()) != json.loads(raw):
            raise ValueError("Captured context changed within one case") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _analysis_runner(root, current):
    if not (CONFIG or {}).get("capture_contexts"):
        return None
    from cloudops.features.diagnosis.worker import analyze

    def before(prepared, _deployment, _request_digest, _byte_size):
        capture_context(root, current["case"], prepared)

    return lambda *args: analyze(*args, before_request=before)


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
        if repair and self.data["repairs"] >= MAX_REPAIRS:
            raise ValueError("Shared repair reserve exhausted")
        count = sum(call["model"] == model for call in self.data["calls"])
        if count >= (PRIMARY_LIMIT if model == MODELS[0] else 1):
            raise ValueError("Per-model request budget exhausted")
        if CONFIG is not None and (
            self.data.get("reserved_usd", 0) + CONFIG["per_request_reserve_usd"]
            > CONFIG["max_reserved_usd"] + 1e-12
        ):
            raise ValueError("Reserved spend ceiling reached")

    def reserve(self, model, case, repair):
        self.check(model, repair)
        self.data["calls"].append(
            {"model": model, "case": case, "repair": repair, "outcome": "unsettled"}
        )
        self.data["repairs"] += int(repair)
        self.data["total_requests"] = self.data.get("prior_requests", 0) + len(self.data["calls"])
        if CONFIG is not None:
            self.data["reserved_usd"] = (
                self.data.get("reserved_usd", 0) + CONFIG["per_request_reserve_usd"]
            )
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
        if CONFIG is not None and call.usage is not None:
            self.data["estimated_usd"] = (
                self.data.get("estimated_usd", 0)
                + (
                    (call.usage.prompt_tokens or 0) * CONFIG["input_usd_per_mtok"]
                    + (call.usage.completion_tokens or 0) * CONFIG["output_usd_per_mtok"]
                )
                / 1_000_000
            )
        save(self.path, self.data)


def configured_profile(model, operational):
    """Build the exact campaign profile from the snapshot taken when the config loaded."""
    loaded = CONFIG["profile"]
    if loaded.model != model:
        raise ValueError("Unapproved model")
    value = loaded.model_dump(mode="json", by_alias=True)
    value.update(
        mode="operational" if operational else "evaluation_only",
        authorization_id=CONFIG["authorization_id"],
        budget_policy_id=CONFIG["budget_policy_id"],
    )
    profile = ModelProfile.model_validate(value)
    profile._base_dir = loaded._base_dir
    return profile


def configure(root, model, *, operational=False):
    primary = model == MODELS[0]
    if CONFIG is not None:
        profile = configured_profile(model, operational)
        return _install(root, profile, operational)
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
    return _install(root, profile, operational)


def _install(root, profile, operational):
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

    if case["id"] != "n-required-gap":
        budget.check(MODELS[0], False)
    run_id = seed_case(db, actor, case)
    analysis = admit(
        db, db.get(InvestigationRun, run_id), purpose="operational" if operational else "evaluation"
    )
    if analysis is None:
        raise ValueError("Analysis admission denied")
    identifier = analysis.id
    db.commit()
    current.update(case=case["id"], repair=False)
    runner = _analysis_runner(budget.path.parent, current)
    if not run_once(run_analysis=runner):
        raise ValueError("Owned worker could not claim admitted case")
    db.expire_all()
    analysis = db.get(Analysis, identifier)
    if (
        analysis.state == "pending"
        and analysis.phase == "repair"
        and budget.data["repairs"] < MAX_REPAIRS
    ):
        current["repair"] = True
        if not run_once(run_analysis=runner):
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


def carried_campaign(path, continue_cases=False):
    previous = json.loads(path.read_text())
    count = previous.get("prior_requests", 0) + len(previous["calls"])
    if count != previous["total_requests"] or not 0 < count < MAX_REQUESTS:
        raise ValueError("Prior campaign request count is invalid")
    if any(not isinstance(call["outcome"], dict) for call in previous["calls"]):
        raise ValueError("Prior campaign has an unsettled call; no automatic replay")
    if previous["models"] != list(MODELS) or previous["endpoint"] != "http://localhost:20128/v1":
        raise ValueError("Prior campaign destination changed")
    carried = {}
    if continue_cases:
        if previous["cases_sha256"] != sha(FIXTURES / "cases.json") or previous[
            "oracle_sha256"
        ] != sha(FIXTURES / "oracle.json"):
            raise ValueError("Continued cases require unchanged inputs and oracle")
        carried = {
            "continued_identity": previous["primary_identity"],
            "cases": {
                key: case
                for key, case in previous["cases"].items()
                if case["purpose"] == "evaluation"
                and (case["calls"] > 0 or case["state"] == "insufficient_evidence")
            },
            "scope_negative": previous["scope_negative"],
        }
    return {
        **carried,
        "prior_requests": count,
        "total_requests": count,
        "observed_tokens": previous["observed_tokens"],
        "missing_usage_calls": previous["missing_usage_calls"],
        "repairs": previous["repairs"],
        "secondary_probe": previous["secondary_probe"],
        "previous_cohort_sha256": sha(path),
        "previous_cohort": previous["cohort"],
    }


def freeze(root, carryover=None, previous=None, continue_cases=False):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (root / "ledger.json").exists():
        raise ValueError("Campaign already exists; never overwrite an initial cohort")
    plan = {
        "cohort": "e06-gpt-structural-v3-owned-fixtures",
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
    if CONFIG is not None:
        if carryover is not None or previous is not None:
            raise ValueError("A configured campaign starts its own history; no carryover")
        plan.update(
            cohort=CONFIG["cohort"],
            models=list(MODELS),
            endpoint=CONFIG["base_url"],
            request_limit=MAX_REQUESTS,
            observed_token_stop=TOKEN_STOP,
            repair_limit=MAX_REPAIRS,
            allowance=(
                f"Owner-delegated allowance: at most {MAX_REQUESTS} requests, "
                f"{MAX_REPAIRS} repairs and {CONFIG['max_reserved_usd']} USD reserved at "
                f"the {CONFIG['rates_date']} rates; no implicit retry"
            ),
            campaign_config_sha256=CONFIG["config_sha256"],
            profile_sha256=CONFIG["profile_sha256"],
            prior_receipts=CONFIG["prior_receipt_hashes"],
            rates={
                "input_usd_per_mtok": CONFIG["input_usd_per_mtok"],
                "output_usd_per_mtok": CONFIG["output_usd_per_mtok"],
                "date": CONFIG["rates_date"],
            },
            per_request_reserve_usd=CONFIG["per_request_reserve_usd"],
            reserved_usd=0,
            estimated_usd=0,
            billed_usd=None,
            secondary_probe={
                "state": "not_configured",
                "quality": "not_established",
                "source": "This configured campaign qualifies one primary model only",
            },
        )
    if carryover is not None and previous is not None:
        raise ValueError("Choose one prior history source")
    if carryover is not None:
        plan.update(carried_probe(carryover))
    if previous is not None:
        plan.update(carried_campaign(previous, continue_cases))
    save(root / "ledger.json", plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["freeze", "run", "smoke"])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--carryover-probe", type=Path)
    parser.add_argument("--carryover-campaign", type=Path)
    parser.add_argument("--continue-cases", action="store_true")
    parser.add_argument("--campaign-config", type=Path)
    args = parser.parse_args()
    root = args.state_dir.resolve()
    if CONFIG is not None:
        reset_config()
    if args.campaign_config is not None:
        load_config(args.campaign_config)
    if args.phase != "freeze" and (root / "ledger.json").exists():
        frozen = json.loads((root / "ledger.json").read_text()).get("campaign_config_sha256")
        if frozen != (CONFIG or {}).get("config_sha256"):
            raise ValueError("Run with the exact campaign config this cohort was frozen with")
        if CONFIG is not None and (
            json.loads((root / "ledger.json").read_text())["profile_sha256"]
            != CONFIG["profile_sha256"]
        ):
            raise ValueError("The campaign profile changed after freezing")
    if args.phase == "freeze":
        freeze(root, args.carryover_probe, args.carryover_campaign, args.continue_cases)
        return
    if (
        make_url(os.environ.get("DATABASE_URL", "")).database != OWNED_DATABASE
        or os.environ.get("E06_OWNED_LAB") != OWNED_LAB
    ):
        raise ValueError(f"Only the explicitly owned {OWNED_DATABASE} lab is allowed")
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
                if budget.data.get("continued_identity", identity) != identity:
                    raise ValueError(
                        "Continued cases require the exact unchanged deployment identity"
                    )
                if args.phase == "smoke":
                    budget.data["smoke_started"] = True
                    save(budget.path, budget.data)
                    case = dict(cases[0], id="operational-smoke")
                    execute_case(db, actor, case, budget, current, operational=True)
                    return
                budget.data.update(started=True, primary_identity=identity)
                save(budget.path, budget.data)
                if "n-required-gap" not in budget.data["cases"]:
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
                if "scope_negative" not in budget.data:
                    scope_negative(db, budget)
                for case in cases:
                    if case["id"] in budget.data["cases"]:
                        continue
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
