"""Immutable dataset snapshots: bundles, labels and the split manifest.

Layout: ``<snapshot>/manifest.json``, ``bundles/<split>/<case>.json`` and
``labels/<split>.json`` (EvalLabels/v1). The sealed test split is read only through
``load(..., split="test", milestone=True)``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from engine_loop import bundle as raw
from engine_loop.bundle import canonical, sha256_json

from cloudops.features.diagnosis.versions import FAMILY_REQUIRED
from cloudops.features.evaluation.schemas import EvalLabel, EvalLabels

MANIFEST_VERSION = "EngineDataset/v1"
SPLITS = ("smoke", "dev", "test")
FAMILIES = tuple(FAMILY_REQUIRED)
HEALTHY_REQUIRED = (
    "kubernetes.state",
    "kubernetes.events",
    "prometheus.request_rate",
    "prometheus.error_ratio",
    "prometheus.p95_latency",
)


class SealedSplitError(PermissionError):
    """The test split is sealed outside a milestone report."""


def split_for(case_id: str) -> str:
    """Deterministic assignment, decided at generation time: 70% dev, 30% test."""
    return "dev" if int(hashlib.sha256(case_id.encode()).hexdigest(), 16) % 10 < 7 else "test"


def fixture_label(case_id: str, oracle: dict) -> EvalLabel:
    item = oracle[case_id]
    category = item["category"]
    supported = item["required_result"] == "supported"
    if supported:
        required = list(FAMILY_REQUIRED[category])
    elif case_id == "n-healthy":
        required = list(HEALTHY_REQUIRED)
    else:
        required = []
    manual = bool(item["manual_only"])
    allowed = list(item["allowed_candidates"])
    return EvalLabel.model_validate(
        {
            "case_id": case_id,
            "category": category,
            "required_result": item["required_result"],
            "manual_only": manual,
            "allowed_candidates": [] if manual else allowed,
            "candidate_requirement": "required" if allowed and not manual else "forbidden",
            "required_sources": required,
            "relevant_sources": list(item["relevant_sources"]),
            # Runbook citation is reported separately; no B0-B2 context carries runbooks.
            "expected_runbooks": [],
            "clean_control": item.get("clean_control"),
            "action": {"state": "not_applicable", "expected_outcome": None},
            "incident": {"state": "not_applicable", "expected_disposition": None},
            "rationale": "E06 controlled fixture label from oracle.json; T0 smoke only.",
        }
    )


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_snapshot(
    out: Path,
    *,
    snapshot_id: str,
    cases: list[tuple[raw.RawEvidenceBundle, EvalLabel, str, dict]],
    description: str,
    pilot: list[str],
) -> dict:
    """Freeze bundles and labels. ``cases`` holds (bundle, label, split, meta) tuples."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"snapshot exists and is immutable: {out}")
    _private_dir(out)
    entries = {}
    labels: dict[str, dict[str, EvalLabel]] = {}
    for value, label, split, meta in cases:
        if split not in SPLITS:
            raise ValueError(f"unknown split {split}")
        if value.case_id != label.case_id:
            raise ValueError("label and bundle case ids differ")
        raw.verify(value)
        path = out / "bundles" / split / f"{value.case_id}.json"
        raw.write(path, value)
        labels.setdefault(split, {})[value.case_id] = label
        entry = {
            "split": split,
            "bundle_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "manifest_sha256": value.manifest_sha256,
        }
        if split != "test":
            entry.update(meta)
        entries[value.case_id] = entry
    label_digests = {}
    for split, items in labels.items():
        document = EvalLabels(cohort_id=snapshot_id, cases=items)
        path = out / "labels" / f"{split}.json"
        _private_dir(path.parent)
        path.write_text(canonical(document.model_dump(mode="json")) + "\n", encoding="utf-8")
        label_digests[split] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "description": description,
        "split_rule": "sha256(case_id) mod 10 < 7 -> dev, else test; fixtures -> smoke",
        "cases": dict(sorted(entries.items())),
        "labels_sha256": label_digests,
        "pilot": pilot,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for path in out.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o400)
    return manifest


def manifest(snapshot: Path) -> dict:
    return json.loads((Path(snapshot) / "manifest.json").read_text(encoding="utf-8"))


def snapshot_digest(snapshot: Path) -> str:
    return hashlib.sha256((Path(snapshot) / "manifest.json").read_bytes()).hexdigest()


def load(
    snapshot: Path, split: str, *, milestone: bool = False, cases: list[str] | None = None
) -> list[tuple[raw.RawEvidenceBundle, EvalLabel]]:
    if split == "test" and not milestone:
        raise SealedSplitError("the test split is sealed; use the milestone report")
    snapshot = Path(snapshot)
    info = manifest(snapshot)
    label_path = snapshot / "labels" / f"{split}.json"
    digest = hashlib.sha256(label_path.read_bytes()).hexdigest()
    if digest != info["labels_sha256"][split]:
        raise ValueError(f"label file changed after freeze: {label_path}")
    labels = EvalLabels.model_validate_json(label_path.read_bytes()).cases
    wanted = [
        case_id
        for case_id, entry in info["cases"].items()
        if entry["split"] == split and (cases is None or case_id in cases)
    ]
    if cases is not None and set(cases) - set(wanted):
        raise KeyError(f"cases not in split {split}: {sorted(set(cases) - set(wanted))}")
    result = []
    for case_id in wanted:
        path = snapshot / "bundles" / split / f"{case_id}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != info["cases"][case_id]["bundle_sha256"]:
            raise ValueError(f"bundle changed after freeze: {path}")
        result.append((raw.read(path), labels[case_id]))
    return result


def fixture_snapshot(out: Path, snapshot_id: str = "e06-fixtures-v1") -> dict:
    oracle = raw.fixture_oracle()
    cases = []
    for case in raw.fixture_cases():
        label = fixture_label(case["id"], oracle)
        meta = {"family": label.category, "variant": "fixture", "source": "e06"}
        cases.append((raw.fixture_bundle(case), label, "smoke", meta))
    return write_snapshot(
        out,
        snapshot_id=snapshot_id,
        cases=cases,
        description="The 12 E06 controlled fixtures (c01-c10, n-healthy, n-injection). T0 smoke.",
        pilot=["c01", "c03", "c05", "c07", "c09", "n-healthy", "n-injection"],
    )


def label_digest(label: EvalLabel) -> str:
    return sha256_json(label.model_dump(mode="json"))
