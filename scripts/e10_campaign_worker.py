"""Run one bounded S6 worker phase against the dedicated, owned lab."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy import select

from cloudops.features.diagnosis.schemas import CallKind, ModelCall
from cloudops.features.diagnosis.settings import load
from cloudops.features.diagnosis.worker import analyze
from cloudops.features.evaluation.hashing import content_sha256
from cloudops.features.evaluation.seal import EvalSealError, verify
from cloudops.features.evaluation.bundle import EvalInputError

from e10_campaign import (
    CampaignError,
    CaseId,
    Digest,
    OWNED_PROJECT,
    RECOVERY_CASES,
    TrialAdmission,
    TrialBinding,
    _private_path,
    _private_state,
    _read,
    _write_once,
    capture,
    check_window,
    read_json,
    verify_source_manifest,
)

SOURCE_MODE = "live_kubernetes,prometheus"
ANALYSIS = "diagnosis_analysis_v1"
JOB_KINDS = {
    "analysis": frozenset({ANALYSIS}),
    "collection": frozenset({"development_collection_v1"}),
    "recovery": frozenset(
        {
            "recovery_execution_v1",
            "recovery_reconciliation_v1",
            "recovery_verification_v1",
        }
    ),
}


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["E10S6Authorization/v1"]
    approval_reference: str = Field(min_length=1, max_length=256)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    campaign_source_sha256: Digest
    deployment_digest: Digest
    profile_digest: Digest
    workload_uid: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    max_requests: int = Field(gt=0, le=999)
    max_requests_per_case: int = Field(gt=0, le=3)
    max_tokens: int = Field(gt=0)
    token_reserve_per_request: int = Field(gt=0)
    max_spend_microusd: int = Field(ge=0)
    spend_reserve_microusd_per_request: int = Field(ge=0)


class RequestReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: CaseId
    analysis_id: str
    authorization_sha256: Digest
    bundle_sha256: Digest
    request_sha256: Digest
    reserved_tokens: int
    reserved_spend_microusd: int
    reserved_at: AwareDatetime


class UsageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_sha256: Digest
    kind: CallKind
    http_status: int | None
    observed_tokens: int | None
    model_matches: bool
    stop_reason: str | None
    received_at: AwareDatetime


class ProductResultReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_id: UUID
    bundle_sha256: Digest
    context_digest: Digest
    result_sha256: Digest


def observed_trial(path: Path) -> dict:
    trial = read_json(path)
    if (
        (trial.get("scope") or {}).get("project") != OWNED_PROJECT
        or trial.get("scenario_kind") != "recovery"
        or trial.get("status") != "observed"
        or not (trial.get("qualification") or {}).get("profile_digest")
        or (trial.get("load") or {}).get("state") != "running"
    ):
        raise CampaignError("active_trial_binding_mismatch")
    return trial


def check_lab(path: Path, admission: TrialAdmission) -> dict:
    trial = observed_trial(path)
    try:
        matches = (
            admission.case_id in RECOVERY_CASES[trial["scenario_id"]]
            and str(admission.trial_id) == trial["trial_id"]
            and str(admission.lab_run_id) == trial["run_id"]
            and str(admission.lab_window_id) == trial["window_id"]
            and admission.workload_uid == trial["target_uid"]
            and admission.source_binding_sha256 == trial["source_binding_sha256"]
        )
    except (KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise CampaignError("active_trial_binding_mismatch")
    check_window(trial, admission.window_start, admission.window_end)
    return trial


class RequestGuard:
    def __init__(
        self,
        state_dir: Path,
        authorization: Authorization,
        admission: TrialAdmission,
        active_trial: Path,
        authority_dir: Path,
    ):
        self.state = _private_state(state_dir)
        self.authorization = authorization
        self.admission = admission
        self.active_trial = active_trial
        self.authorization_sha = content_sha256(authorization)
        self.directory = _private_path(_private_state(authority_dir), "provider")
        self.pending: tuple[Path, RequestReceipt] | None = None
        self.captured = None
        self.stop_reason: str | None = None

    def _check_deployment(self, deployment) -> None:
        auth = self.authorization
        if datetime.now(timezone.utc) >= auth.expires_at:
            raise CampaignError("authorization_expired")
        if (
            deployment.denied() is not None
            or content_sha256(deployment.identity()) != auth.deployment_digest
            or content_sha256(asdict(deployment.profile.identity())) != auth.profile_digest
            or self.admission.profile_digest != auth.profile_digest
            or self.admission.workload_uid != auth.workload_uid
        ):
            raise CampaignError("authorization_binding_mismatch")

    def _check_source(self) -> None:
        seal = verify(self.state, include_scoring_inputs=False)
        if (
            seal.source_revision != self.authorization.source_revision
            or verify_source_manifest(self.state) != self.authorization.campaign_source_sha256
        ):
            raise CampaignError("authorization_source_mismatch")

    def preflight(self) -> None:
        self._check_source()
        self._check_deployment(load())
        check_lab(self.active_trial, self.admission)

    def _reserve(self, context, deployment, request_sha: str, request_bytes: int) -> None:
        self._check_source()
        self._check_deployment(deployment)
        check_lab(self.active_trial, self.admission)
        auth = self.authorization
        output_limit = deployment.profile.output_limit.value
        if output_limit is None or request_bytes + output_limit > auth.token_reserve_per_request:
            raise CampaignError("request_bound_exceeds_token_reserve")
        reserved_tokens = reserved_spend = count = case_count = 0
        for file in sorted(self.directory.glob("*.request.json")):
            receipt = _read(file, RequestReceipt, "provider_ledger_invalid")
            usage = _read(
                file.with_name(file.name.replace(".request.", ".usage.")),
                UsageReceipt,
                "unsettled_provider_request",
            )
            if (
                receipt.authorization_sha256 != self.authorization_sha
                or usage.reservation_sha256 != content_sha256(receipt)
            ):
                raise CampaignError("provider_ledger_binding_mismatch")
            if usage.stop_reason is not None:
                raise CampaignError(usage.stop_reason)
            count += 1
            case_count += receipt.case_id == self.admission.case_id
            reserved_tokens += receipt.reserved_tokens
            reserved_spend += receipt.reserved_spend_microusd
        if (
            count >= auth.max_requests
            or case_count >= auth.max_requests_per_case
            or reserved_tokens + auth.token_reserve_per_request > auth.max_tokens
            or reserved_spend + auth.spend_reserve_microusd_per_request > auth.max_spend_microusd
        ):
            raise CampaignError("provider_bounds_reached")

        binding = TrialBinding(**self.admission.model_dump(), context_digest=context.digest)
        captured = capture(self.state, binding, context)
        receipt = RequestReceipt(
            case_id=binding.case_id,
            analysis_id=str(binding.analysis_id),
            authorization_sha256=self.authorization_sha,
            bundle_sha256=captured.bundle_sha256,
            request_sha256=request_sha,
            reserved_tokens=auth.token_reserve_per_request,
            reserved_spend_microusd=auth.spend_reserve_microusd_per_request,
            reserved_at=datetime.now(timezone.utc),
        )
        _write_once(self.directory / f"{count + 1:04d}.request.json", receipt)
        self.captured = captured
        self.pending = (self.directory / f"{count + 1:04d}.usage.json", receipt)

    def before_request(self, context, deployment, request_sha: str, request_bytes: int) -> None:
        try:
            self._reserve(context, deployment, request_sha, request_bytes)
        except (CampaignError, EvalInputError, EvalSealError) as error:
            self.stop_reason = str(error)
            raise
        except (OSError, ValidationError):
            self.stop_reason = "provider_reservation_io_failure"
            raise

    def after_request(self, call: ModelCall, deployment) -> None:
        if self.pending is None:
            self.stop_reason = "provider_preflight_failed"
            return
        path, request = self.pending
        usage = call.usage
        observed = (
            max(usage.total_tokens, (usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
            if usage is not None and usage.total_tokens is not None
            else None
        )
        model_matches = call.served_model == deployment.profile.model
        if observed is None:
            self.stop_reason = "provider_usage_unavailable"
        elif observed > request.reserved_tokens:
            self.stop_reason = "provider_token_bound_exceeded"
        elif call.kind != CallKind.TEXT or not model_matches:
            self.stop_reason = "provider_response_denied"
        result = UsageReceipt(
            reservation_sha256=content_sha256(request),
            kind=call.kind,
            http_status=call.http_status,
            observed_tokens=observed,
            model_matches=model_matches,
            stop_reason=self.stop_reason,
            received_at=datetime.now(timezone.utc),
        )
        try:
            _write_once(path, result)
        except (CampaignError, OSError):
            self.stop_reason = "provider_usage_io_failure"
        else:
            self.pending = None

    def analyze(self, claim, gate, slot, lease_lost):
        if claim.subject_id != self.admission.analysis_id:
            self.stop_reason = "unexpected_analysis_claim"
            raise CampaignError(self.stop_reason)
        analyze(
            claim,
            gate,
            slot,
            lease_lost,
            before_request=self.before_request,
            after_request=self.after_request,
        )
        if self.pending is not None:
            self.stop_reason = "unsettled_provider_request"

    def record_result(self, payload: dict) -> None:
        captured = self.captured
        if (
            captured is None
            or payload.get("context_digest") != captured.context_digest
            or payload.get("run_id") != str(self.admission.run_id)
            or payload.get("incident_id") != str(self.admission.incident_id)
        ):
            raise CampaignError("product_result_binding_mismatch")
        receipt = ProductResultReceipt(
            analysis_id=self.admission.analysis_id,
            bundle_sha256=captured.bundle_sha256,
            context_digest=captured.context_digest,
            result_sha256=content_sha256(payload),
        )
        _write_once(self.directory / f"{self.admission.case_id}.result.json", receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--opening", type=Path)
    parser.add_argument("--plan", type=UUID)
    parser.add_argument("--active-trial", type=Path, required=True)
    parser.add_argument("--phase", choices=JOB_KINDS, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    if (
        not 1 <= args.seconds <= 900
        or (args.authorization is not None) != (args.phase == "analysis")
        or (args.admission is not None) != (args.phase in {"analysis", "recovery"})
        or (args.opening is not None) != (args.phase == "collection")
        or (args.plan is not None) != (args.phase == "recovery")
        or os.name != "posix"
    ):
        parser.error("Use Linux, 1–900 seconds and the inputs required by this phase.")

    import fcntl

    from cloudops.entrypoints.worker import run_once
    from cloudops.features.diagnosis.lifecycle import ACTIVE
    from cloudops.features.diagnosis.models import Analysis, AnalysisResult
    from cloudops.features.incidents.models import Incident
    from cloudops.features.investigations.models import InvestigationRun
    from cloudops.features.recovery.models import ActionPlan, Execution
    from cloudops.features.workloads.models import Workload
    from cloudops.shared.database import require_schema, sessions

    try:
        if (
            os.environ.get("CLOUDOPS_LAB_PROJECT") != OWNED_PROJECT
            or os.environ.get("COLLECTION_SOURCE_MODE") != SOURCE_MODE
            or make_url(os.environ.get("DATABASE_URL", "")).database != "cloudops_lab"
        ):
            raise CampaignError("owned_lab_environment_required")
        state = _private_state(args.state_dir)
        lock = _private_path(state, "campaign/worker.lock")
        lock.parent.mkdir(exist_ok=True, mode=0o700)
        with ExitStack() as locks:
            stream = locks.enter_context(lock.open("ab"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require_schema()
            guard = None
            kinds = JOB_KINDS[args.phase]
            admission = None
            if args.admission is not None:
                admission = _read(args.admission, TrialAdmission, "trial_admission_invalid")
                trial = check_lab(args.active_trial, admission)
            else:
                trial = observed_trial(args.active_trial)

            if args.phase == "collection":
                opening = read_json(args.opening)
                subject = UUID(opening["run_id"])
                if opening["trial_id"] != trial["trial_id"]:
                    raise CampaignError("collection_opening_binding_mismatch")
                with sessions()() as db:
                    run = db.get(InvestigationRun, subject)
                    incident = db.get(Incident, run.incident_id) if run else None
                    workload = db.get(Workload, incident.workload_id) if incident else None
                    if (
                        run is None
                        or str(run.incident_id) != opening["incident_id"]
                        or workload is None
                        or workload.uid != trial["target_uid"]
                    ):
                        raise CampaignError("collection_run_binding_mismatch")
                    check_window(trial, run.window_start, run.window_end)
            elif args.phase == "analysis":
                subject = admission.analysis_id
            else:
                with sessions()() as db:
                    plan = db.get(ActionPlan, args.plan)
                    if (
                        plan is None
                        or plan.state != "approved"
                        or plan.incident_id != admission.incident_id
                        or plan.source_analysis_id != admission.analysis_id
                        or plan.content["workload"]["uid"] != admission.workload_uid
                        or plan.content.get("verification_profile_digest")
                        != trial["qualification"]["profile_digest"]
                    ):
                        raise CampaignError("recovery_plan_binding_mismatch")
                    subject = db.scalar(select(Execution.id).where(Execution.plan_id == args.plan))
                    if subject is None:
                        raise CampaignError("recovery_execution_missing")

            if args.authorization is not None:
                authority_dir = _private_state(args.authorization.parent)
                if (
                    authority_dir == state
                    or state in authority_dir.parents
                    or authority_dir in state.parents
                    or args.authorization.is_symlink()
                ):
                    raise CampaignError("independent_authority_directory_required")
                authority_lock = _private_path(authority_dir, "worker.lock")
                authority_stream = locks.enter_context(authority_lock.open("ab"))
                fcntl.flock(authority_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                guard = RequestGuard(
                    state,
                    _read(args.authorization, Authorization, "authorization_invalid"),
                    admission,
                    args.active_trial,
                    authority_dir,
                )
                guard.preflight()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                if guard is None:
                    if admission is not None:
                        check_lab(args.active_trial, admission)
                    ran = run_once(job_kinds=kinds, subject_id=subject)
                else:
                    ran = run_once(job_kinds=kinds, subject_id=subject, run_analysis=guard.analyze)
                    if guard.stop_reason or guard.pending is not None:
                        raise CampaignError(guard.stop_reason or "unsettled_provider_request")
                    if ran:
                        with sessions()() as db:
                            analysis = db.get(Analysis, admission.analysis_id)
                            if analysis is None or analysis.state not in ACTIVE:
                                result = db.get(AnalysisResult, admission.analysis_id)
                                if result is not None:
                                    guard.record_result(result.payload)
                                return 0
                if not ran:
                    time.sleep(1)
    except (CampaignError, EvalSealError, EvalInputError) as error:
        parser.exit(1, f"e10-campaign-worker: {error}\n")
    except Exception:
        parser.exit(1, "e10-campaign-worker: owned runtime or artifact I/O failure\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
