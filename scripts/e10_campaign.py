"""Capture private S6 evidence and retain trial dispositions alongside the S4 ledger."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

from cloudops.features.diagnosis.context import PreparedContext, recomputed_digest
from cloudops.features.evaluation import bundle
from cloudops.features.evaluation.hashing import content_sha256
from cloudops.features.evaluation.seal import EvalSealError, load_cohort, verify

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_LAB_PROJECT = "cloudops-e10-s3-lab"
# The campaign's recovery lab: the source-built E10 S3 lab, or a new owned recovery lab run
# from a verified release bundle (scripts/release_scenarios.py), selected by
# CLOUDOPS_LAB_PROJECT. Any other value keeps the source-built lab, so its checks fail closed.
_SELECTED_PROJECT = os.environ.get("CLOUDOPS_LAB_PROJECT", RECOVERY_LAB_PROJECT)
OWNED_PROJECT = (
    _SELECTED_PROJECT
    if re.fullmatch(r"cloudops-release-recovery-[a-z0-9]{1,24}", _SELECTED_PROJECT)
    else RECOVERY_LAB_PROJECT
)
SOURCE_MANIFEST = "campaign-source.json"
S6_SOURCES = (
    "compose.yaml",
    "compose.https.yaml",
    "compose.integration.yaml",
    "compose.alertmanager.yaml",
    "compose.lab.yaml",
    "compose.lab-recovery.yaml",
    "compose.lab-release.yaml",
    "compose.diagnosis.yaml",
    "config/lab/release-scenarios-v1.json",
    "config/lab/recovery-audit-policy.yaml",
    "scripts/e10_campaign.py",
    "scripts/e10_campaign_controls.py",
    "scripts/e10_campaign_plan.py",
    "scripts/e10_campaign_report.py",
    "scripts/e10_campaign_timing.py",
    "scripts/e10_campaign_worker.py",
    "scripts/local_lab.py",
    "scripts/manage-local-lab.py",
    "scripts/probe-e10-s3-recovery.py",
    "scripts/release-bundle.py",
    "scripts/release_scenarios.py",
    "services/backend/Dockerfile",
    "services/backend/src/cloudops/entrypoints/worker.py",
    "services/backend/src/cloudops/features/diagnosis/worker.py",
    "services/backend/src/cloudops/shared/jobs.py",
    "services/backend/tests/e06_live_acceptance.py",
    "services/backend/tests/test_diagnosis_validation.py",
    "services/backend/tests/fixtures/e06/cases.json",
    "services/backend/tests/fixtures/e06/oracle.json",
)

CaseId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
Disposition = Literal["completed", "failed", "blocked", "missing", "interrupted"]
RECOVERY_SCENARIOS = (
    "recovery-oom-memory",
    "recovery-bad-rollout-restore",
    "recovery-traffic-scale",
)
RECOVERY_CASES = {
    scenario: tuple(f"{scenario}-{index:02d}" for index in range(1, 6))
    for scenario in RECOVERY_SCENARIOS
}


class CampaignError(ValueError):
    """Closed error code, without the contents of private inputs."""


class CampaignSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["E10S6Source/v1"] = "E10S6Source/v1"
    seal_sha256: Digest
    files: dict[str, Digest]


class TrialAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: CaseId
    trial_id: UUID
    lab_run_id: UUID
    lab_window_id: UUID
    incident_id: UUID
    run_id: UUID
    analysis_id: UUID
    workload_uid: str = Field(min_length=1, max_length=128)
    window_start: AwareDatetime
    window_end: AwareDatetime
    profile_digest: Digest
    source_binding_sha256: Digest

    @model_validator(mode="after")
    def ordered_window(self):
        if self.window_start >= self.window_end:
            raise ValueError("window_start must precede window_end")
        return self


class TrialBinding(TrialAdmission):
    context_digest: Digest


class CaptureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["E10S6CaptureReceipt/v1"] = "E10S6CaptureReceipt/v1"
    binding: TrialBinding
    bundle_sha256: Digest
    context_digest: Digest


class TrialOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["E10S6TrialOutcome/v1"] = "E10S6TrialOutcome/v1"
    case_id: CaseId
    disposition: Disposition
    reason: Reason | None
    bundle_sha256: Digest | None
    reset_sha256: Digest | None = None
    recorded_at: AwareDatetime


def _read(path: Path, model, code: str):
    try:
        if not path.is_file() or path.stat().st_size > bundle.MAX_BUNDLE_BYTES:
            raise CampaignError(code)
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        raise CampaignError(code) from None


def _private_path(root: Path, relative: str) -> Path:
    path = root / relative
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise CampaignError("path_invalid")
        current = current.parent
    return path


def _private_state(state_dir: Path) -> Path:
    root = state_dir.resolve(strict=True)
    if not root.is_dir() or (os.name == "posix" and root.stat().st_mode & 0o077):
        raise CampaignError("private_state_directory_required")
    return root


def _write_once(path: Path, value: BaseModel) -> None:
    created = []
    folder = path.parent
    while not folder.exists():
        created.append(folder)
        folder = folder.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (value.model_dump_json() + "\n").encode("utf-8")
    if len(raw) > bundle.MAX_BUNDLE_BYTES:
        raise CampaignError("artifact_too_large")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        if os.name == "posix":
            for folder in (path.parent, *(entry.parent for entry in created)):
                directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    except FileExistsError:
        raise CampaignError("artifact_exists") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def source_manifest(state_dir: Path) -> CampaignSource:
    backend = ROOT / "services/backend/src"
    source_paths = set(S6_SOURCES)
    source_paths.update(path.relative_to(ROOT).as_posix() for path in backend.rglob("*.py"))
    return CampaignSource(
        seal_sha256=hashlib.sha256((state_dir / "seal.json").read_bytes()).hexdigest(),
        files={
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in sorted(source_paths)
        },
    )


def verify_source_manifest(state_dir: Path) -> str:
    stored = _read(
        _private_path(state_dir, SOURCE_MANIFEST), CampaignSource, "campaign_source_invalid"
    )
    if stored != source_manifest(state_dir):
        raise CampaignError("campaign_source_drift")
    return content_sha256(stored)


def _case(state_dir: Path, case_id: str):
    seal = verify(state_dir, include_scoring_inputs=False)
    case = next((entry for entry in load_cohort(state_dir).cases if entry.case_id == case_id), None)
    if case is None:
        raise CampaignError("case_not_planned")
    if case.bundle in seal.files:
        raise CampaignError("bundle_frozen_at_seal")
    return case


def _check_context(binding: TrialBinding, context: PreparedContext) -> None:
    if (
        context.incident_id != binding.incident_id
        or context.run_id != binding.run_id
        or context.analysis_identity != str(binding.analysis_id)
        or context.workload_uid != binding.workload_uid
        or context.window.start != binding.window_start
        or context.window.end != binding.window_end
        or context.digest != binding.context_digest
        or recomputed_digest(context) != context.digest
        or content_sha256(asdict(context.profile_identity)) != binding.profile_digest
    ):
        raise CampaignError("context_binding_mismatch")


def read_json(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(bundle.MAX_BUNDLE_BYTES + 1)
        if len(raw) > bundle.MAX_BUNDLE_BYTES:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise CampaignError("artifact_invalid") from None


def check_window(trial: dict, start: datetime, end: datetime) -> None:
    try:
        baseline = datetime.fromisoformat(trial["phases"]["baseline"])
        injection = datetime.fromisoformat(trial["phases"]["inject"])
        detection = datetime.fromisoformat(trial["phases"]["detect"])
        load = trial["load"]
        expiry = datetime.fromisoformat(load["started_at"]) + timedelta(seconds=load["seconds"])
        now = datetime.now(timezone.utc)
        if baseline <= start <= injection <= detection <= end <= now < expiry:
            return
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    raise CampaignError("trial_window_or_load_mismatch")


def bound_reset(path: Path, binding: TrialBinding) -> dict:
    from release_scenarios import validate_trial_receipt

    try:
        receipt = validate_trial_receipt(read_json(path))
    except (KeyError, TypeError, ValueError):
        raise CampaignError("reset_receipt_invalid") from None
    if (
        receipt["scope"]["project"] != OWNED_PROJECT
        or binding.case_id not in RECOVERY_CASES.get(receipt["scenario_id"], ())
        or receipt["trial_id"] != str(binding.trial_id)
        or receipt["run_id"] != str(binding.lab_run_id)
        or receipt["window_id"] != str(binding.lab_window_id)
        or receipt["target_uid"] != binding.workload_uid
        or receipt["source_binding_sha256"] != binding.source_binding_sha256
        or receipt["phases"].get("reset") is None
        or (receipt.get("evidence") or {}).get("lab_state") != "restored_baseline"
        or receipt["residual_eligible_actions"] != 0
        or receipt["duplicate_targeted_loads"] != 0
    ):
        raise CampaignError("reset_receipt_binding_mismatch")
    return receipt


def _reserve(state_dir: Path, path: Path, binding: TrialBinding) -> None:
    identities = ("trial_id", "lab_run_id", "lab_window_id", "incident_id", "run_id", "analysis_id")
    for other_path in path.parent.glob("*.json"):
        relative = other_path.relative_to(state_dir).as_posix()
        other = _read(_private_path(state_dir, relative), TrialBinding, "binding_invalid")
        if any(getattr(binding, field) == getattr(other, field) for field in identities):
            raise CampaignError("trial_identity_reused")
    _write_once(path, binding)


def capture(state_dir: Path, binding: TrialBinding, context: PreparedContext) -> CaptureReceipt:
    """Publish a late E04/E05 bundle at its planned S4 path and bind it to one trial."""
    state_dir = _private_state(state_dir)
    case = _case(state_dir, binding.case_id)
    bundle_path = _private_path(state_dir, case.bundle)
    receipt_path = _private_path(state_dir, f"campaign/captures/{case.case_id}.json")
    reservation = _private_path(state_dir, f"campaign/bindings/{case.case_id}.json")
    closed = _private_path(state_dir, f"campaign/outcomes/{case.case_id}.json")
    _check_context(binding, context)
    if closed.exists():
        raise CampaignError("trial_closed")
    if reservation.exists():
        if _read(reservation, TrialBinding, "binding_invalid") != binding:
            raise CampaignError("trial_binding_mismatch")
    else:
        if bundle_path.exists() or receipt_path.exists():
            raise CampaignError("unbound_artifact")
        _reserve(state_dir, reservation, binding)
    if not bundle_path.exists():
        if receipt_path.exists():
            raise CampaignError("captured_bundle_missing")
        value = bundle.from_context(
            context,
            case_id=case.case_id,
            bundle_id=str(binding.trial_id),
            captured_at=datetime.now(timezone.utc),
        )
        _write_once(bundle_path, value)
    retained, raw_sha = bundle.load(bundle_path, expected_case_id=case.case_id)
    if retained.bundle_id != str(binding.trial_id) or retained.context != context:
        raise CampaignError("captured_context_changed")
    receipt = CaptureReceipt(binding=binding, bundle_sha256=raw_sha, context_digest=context.digest)
    if receipt_path.exists():
        if _read(receipt_path, CaptureReceipt, "capture_invalid") != receipt:
            raise CampaignError("capture_binding_mismatch")
    else:
        _write_once(receipt_path, receipt)
    return receipt


def record_outcome(
    state_dir: Path,
    case_id: str,
    disposition: Disposition,
    reason: str | None,
    *,
    reset_receipt: Path | None = None,
) -> TrialOutcome:
    """Retain a terminal trial independently of whether either method could be scored."""
    state_dir = _private_state(state_dir)
    case = _case(state_dir, case_id)
    if (disposition == "completed") != (reason is None):
        raise CampaignError("outcome_reason_required_for_noncompletion")
    capture_path = _private_path(state_dir, f"campaign/captures/{case_id}.json")
    bundle_path = _private_path(state_dir, case.bundle)
    if bundle_path.exists() != capture_path.exists():
        raise CampaignError("capture_incomplete")
    raw_sha = None
    reset_sha = None
    if capture_path.exists():
        receipt = _read(capture_path, CaptureReceipt, "capture_invalid")
        binding_path = _private_path(state_dir, f"campaign/bindings/{case_id}.json")
        binding = _read(binding_path, TrialBinding, "binding_invalid")
        retained, raw_sha = bundle.load(bundle_path, expected_case_id=case_id)
        _check_context(binding, retained.context)
        if (
            binding.case_id != case_id
            or receipt.binding != binding
            or retained.bundle_id != str(binding.trial_id)
            or receipt.bundle_sha256 != raw_sha
            or receipt.context_digest != retained.context.digest
        ):
            raise CampaignError("capture_binding_mismatch")
        if reset_receipt is not None:
            reset = bound_reset(reset_receipt, binding)
            reset_sha = content_sha256(reset)
            archive = _private_path(state_dir, f"campaign/resets/{case_id}.json")
            if archive.exists():
                if content_sha256(read_json(archive)) != reset_sha:
                    raise CampaignError("reset_receipt_binding_mismatch")
            else:
                _write_once(archive, RootModel[dict](reset))
    elif reset_receipt is not None:
        raise CampaignError("reset_receipt_requires_capture")
    elif disposition == "completed":
        raise CampaignError("completed_trial_requires_capture")

    value = TrialOutcome(
        case_id=case_id,
        disposition=disposition,
        reason=reason,
        bundle_sha256=raw_sha,
        reset_sha256=reset_sha,
        recorded_at=datetime.now(timezone.utc),
    )
    _write_once(_private_path(state_dir, f"campaign/outcomes/{case_id}.json"), value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("capture")
    archive.add_argument("--state-dir", type=Path, required=True)
    archive.add_argument("--binding", type=Path, required=True)
    archive.add_argument("--context", type=Path, required=True)
    record = commands.add_parser("record-outcome")
    record.add_argument("--state-dir", type=Path, required=True)
    record.add_argument("--case-id", required=True)
    record.add_argument(
        "--disposition",
        choices=("completed", "failed", "blocked", "missing", "interrupted"),
        required=True,
    )
    record.add_argument("--reason")
    record.add_argument("--reset-receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            binding = _read(args.binding, TrialBinding, "binding_invalid")
            context = _read(args.context, PreparedContext, "context_invalid")
            capture(args.state_dir, binding, context)
        else:
            record_outcome(
                args.state_dir,
                args.case_id,
                args.disposition,
                args.reason,
                reset_receipt=args.reset_receipt,
            )
    except (CampaignError, EvalSealError, bundle.EvalInputError) as error:
        parser.exit(1, f"e10-campaign: {error}\n")
    except (OSError, ValidationError):
        parser.exit(1, "e10-campaign: invalid input or artifact I/O failure\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
