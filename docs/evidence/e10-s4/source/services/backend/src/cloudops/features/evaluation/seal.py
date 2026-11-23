"""Pre-measurement input seal and append-only evaluation ledger."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from cloudops.features.evaluation.schemas import (
    EvalCaseLedger,
    EvalCohort,
    EvalLabels,
    EvalLedger,
    EvalRubric,
    EvalSeal,
)
from cloudops.features.evaluation.versions import IDENTITY_FIELDS, VERSIONS

REPO_ROOT = Path(__file__).resolve().parents[6]
_PINNED_SOURCES = (
    "services/backend/src/cloudops/features/evaluation/__init__.py",
    "services/backend/src/cloudops/features/evaluation/versions.py",
    "services/backend/src/cloudops/features/evaluation/hashing.py",
    "services/backend/src/cloudops/features/evaluation/schemas.py",
    "services/backend/src/cloudops/features/evaluation/bundle.py",
    "services/backend/src/cloudops/features/evaluation/rules.py",
    "services/backend/src/cloudops/features/evaluation/scoring.py",
    "services/backend/src/cloudops/features/evaluation/summary.py",
    "services/backend/src/cloudops/features/evaluation/seal.py",
    "services/backend/src/cloudops/features/evaluation/cli.py",
    "services/backend/src/cloudops/features/diagnosis/advice.py",
    "services/backend/src/cloudops/features/diagnosis/context.py",
    "services/backend/src/cloudops/features/diagnosis/egress.py",
    "services/backend/src/cloudops/features/diagnosis/messages.py",
    "services/backend/src/cloudops/features/diagnosis/profile.py",
    "services/backend/src/cloudops/features/diagnosis/schemas.py",
    "services/backend/src/cloudops/features/diagnosis/versions.py",
    "services/backend/src/cloudops/features/investigations/catalog.py",
    "services/backend/src/cloudops/features/investigations/locators.py",
    "services/backend/src/cloudops/features/investigations/citation_schemas.py",
    "services/backend/src/cloudops/features/investigations/models.py",
    "services/backend/src/cloudops/features/investigations/service.py",
    "services/backend/src/cloudops/features/investigations/storage.py",
    "services/backend/src/cloudops/features/knowledge/guidance.py",
    "services/backend/src/cloudops/features/knowledge/guidance_schemas.py",
    "services/backend/src/cloudops/features/knowledge/schemas.py",
    "services/backend/tests/diagnosis_fixtures.py",
    "services/backend/tests/fixtures/e06/cases.json",
    "services/backend/tests/fixtures/e06/oracle.json",
    "runbooks/catalog.json",
    "runbooks/crashloop-startup.md",
    "runbooks/image-pull.md",
    "runbooks/oom-restarts.md",
    "runbooks/pending-unschedulable.md",
    "runbooks/restore-deployment-template.md",
    "runbooks/scale-deployment.md",
    "runbooks/set-memory-profile.md",
    "runbooks/traffic-latency-errors.md",
    "services/backend/pyproject.toml",
    "services/backend/uv.lock",
    "services/backend/tests/eval_fixtures.py",
    "scripts/eval-baseline.py",
    "scripts/build-eval-fixtures.py",
)
_SCORING_ONLY_SOURCES = frozenset(
    {
        "services/backend/tests/eval_fixtures.py",
        "services/backend/tests/diagnosis_fixtures.py",
        "services/backend/tests/fixtures/e06/cases.json",
        "services/backend/tests/fixtures/e06/oracle.json",
    }
)


class EvalSealError(ValueError):
    """Closed seal or ledger failure without artifact contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_state_file(state_dir: Path, relative: str, *, must_exist: bool = True) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise EvalSealError("path_invalid")
    root = state_dir.resolve()
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError:
        raise EvalSealError("file_missing") from None
    if resolved != root and root not in resolved.parents:
        raise EvalSealError("path_invalid")
    if candidate.is_symlink():
        raise EvalSealError("path_invalid")
    return candidate


def _load_model(path: Path, model_type, code: str):
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        return model_type.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValidationError):
        raise EvalSealError(code) from None


def load_cohort(state_dir: Path) -> EvalCohort:
    root = Path(state_dir)
    return _load_model(root / "inputs/cohort.json", EvalCohort, "cohort_invalid")


def load_inputs(state_dir: Path) -> tuple[EvalCohort, EvalLabels, EvalRubric]:
    root = Path(state_dir)
    cohort = load_cohort(root)
    labels = _load_model(root / "inputs/labels.json", EvalLabels, "labels_invalid")
    rubric = _load_model(root / "inputs/rubric.json", EvalRubric, "rubric_invalid")
    if cohort.cohort_id != labels.cohort_id or cohort.cohort_id != rubric.cohort_id:
        raise EvalSealError("cohort_mismatch")
    if set(labels.cases) != {case.case_id for case in cohort.cases}:
        raise EvalSealError("label_case_mismatch")
    return cohort, labels, rubric


def load_ledger(state_dir: Path) -> EvalLedger:
    root = Path(state_dir)
    return _load_model(root / "ledger.json", EvalLedger, "ledger_invalid")


def write_ledger(state_dir: Path, ledger: EvalLedger) -> None:
    _write_atomic(Path(state_dir) / "ledger.json", _json_bytes(ledger.model_dump(mode="json")))


def freeze(
    state_dir: Path,
    cohort_path: Path,
    labels_path: Path,
    rubric_path: Path,
    source_revision: str,
) -> EvalSeal:
    state_dir = Path(state_dir)
    if state_dir.exists() and (not state_dir.is_dir() or any(state_dir.iterdir())):
        raise EvalSealError("state_exists")
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        cohort_bytes = Path(cohort_path).read_bytes()
        labels_bytes = Path(labels_path).read_bytes()
        rubric_bytes = Path(rubric_path).read_bytes()
        cohort = EvalCohort.model_validate_json(cohort_bytes)
        labels = EvalLabels.model_validate_json(labels_bytes)
        rubric = EvalRubric.model_validate_json(rubric_bytes)
    except (OSError, ValidationError, ValueError):
        raise EvalSealError("input_invalid") from None
    if (
        cohort.cohort_id != labels.cohort_id
        or cohort.cohort_id != rubric.cohort_id
        or set(labels.cases) != {case.case_id for case in cohort.cases}
    ):
        raise EvalSealError("cohort_mismatch")
    if len(source_revision) != 40 or any(
        char not in "0123456789abcdef" for char in source_revision
    ):
        raise EvalSealError("source_revision_invalid")

    files: dict[str, str] = {}
    for name, raw in (
        ("inputs/cohort.json", cohort_bytes),
        ("inputs/labels.json", labels_bytes),
        ("inputs/rubric.json", rubric_bytes),
    ):
        target = _safe_state_file(state_dir, name, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        files[name] = _sha(raw)

    source_root = Path(cohort_path).resolve().parent
    for case in cohort.cases:
        source_bundle = source_root.joinpath(*PurePosixPath(case.bundle).parts)
        if not source_bundle.exists():
            continue
        if source_bundle.is_symlink() or source_root not in source_bundle.resolve().parents:
            raise EvalSealError("path_invalid")
        raw = source_bundle.read_bytes()
        target = _safe_state_file(state_dir, case.bundle, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        files[case.bundle] = _sha(raw)

    for relative in _PINNED_SOURCES:
        source = REPO_ROOT / Path(relative)
        try:
            raw = source.read_bytes()
        except OSError:
            raise EvalSealError("source_missing") from None
        destination = f"source/{relative}"
        target = _safe_state_file(state_dir, destination, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        files[destination] = _sha(raw)

    seal = EvalSeal(
        cohort_id=cohort.cohort_id,
        source_revision=source_revision,
        frozen_at=datetime.now(timezone.utc),
        files=files,
        versions=dict(VERSIONS),
        identity_fields=IDENTITY_FIELDS,
    )
    ledger = EvalLedger(
        cohort_id=cohort.cohort_id,
        source_revision=source_revision,
        started=False,
        cases=[
            EvalCaseLedger(
                case_id=case.case_id,
                methods=list(case.methods),
                receipt_sha256={method: None for method in case.methods},
            )
            for case in cohort.cases
        ],
    )
    _write_atomic(state_dir / "seal.json", _json_bytes(seal.model_dump(mode="json")))
    write_ledger(state_dir, ledger)
    return seal


def verify(state_dir: Path, *, include_scoring_inputs: bool = True) -> EvalSeal:
    state_dir = Path(state_dir)
    try:
        raw = (state_dir / "seal.json").read_bytes()
        seal = EvalSeal.model_validate_json(raw)
    except (OSError, ValidationError, ValueError):
        raise EvalSealError("seal_missing") from None
    if seal.versions != VERSIONS or seal.identity_fields != IDENTITY_FIELDS:
        raise EvalSealError("seal_mismatch")
    for relative, expected in seal.files.items():
        if not include_scoring_inputs:
            scoring_only = relative in {"inputs/labels.json", "inputs/rubric.json"} or (
                relative.startswith("source/")
                and relative.removeprefix("source/") in _SCORING_ONLY_SOURCES
            )
            if scoring_only:
                continue
        path = _safe_state_file(state_dir, relative)
        try:
            actual = _sha(path.read_bytes())
        except OSError:
            raise EvalSealError("seal_mismatch") from None
        if actual != expected:
            raise EvalSealError("seal_mismatch")
    for relative in _PINNED_SOURCES:
        if not include_scoring_inputs and relative in _SCORING_ONLY_SOURCES:
            continue
        sealed_path = _safe_state_file(state_dir, f"source/{relative}")
        live_path = REPO_ROOT / Path(relative)
        try:
            sealed_hash = _sha(sealed_path.read_bytes())
            live_hash = _sha(live_path.read_bytes())
        except OSError:
            raise EvalSealError("source_drift") from None
        if live_hash != sealed_hash:
            raise EvalSealError("source_drift")
    cohort = load_cohort(state_dir)
    if cohort.cohort_id != seal.cohort_id:
        raise EvalSealError("seal_mismatch")
    if include_scoring_inputs:
        _, labels, rubric = load_inputs(state_dir)
        if labels.cohort_id != seal.cohort_id or rubric.cohort_id != seal.cohort_id:
            raise EvalSealError("seal_mismatch")
    ledger = load_ledger(state_dir)
    if (
        ledger.cohort_id != seal.cohort_id
        or ledger.source_revision != seal.source_revision
        or {item.case_id: item.methods for item in ledger.cases}
        != {case.case_id: case.methods for case in cohort.cases}
    ):
        raise EvalSealError("ledger_mismatch")
    return seal
