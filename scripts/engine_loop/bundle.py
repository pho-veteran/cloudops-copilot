"""RawEvidenceBundle/v1: stored evidence from before selection, and its offline loader.

A bundle freezes the Run evidence rows exactly as `context.load_materials` reads them, so
every selection and encoding stage can be replayed without PostgreSQL. Labels are never
part of a bundle.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from cloudops.features.diagnosis.context import ContextScope, SlotMaterial
from cloudops.features.diagnosis.versions import SLOT_CAPS, SOURCE_CLASSES
from cloudops.features.investigations.locators import UnsupportedProjection, project
from cloudops.features.knowledge.guidance_schemas import KnowledgeRunScope

BUNDLE_VERSION = "RawEvidenceBundle/v1"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "services/backend/tests/fixtures/e06"
# The E06 fixture window; identical to tests/eval_fixtures so contexts reproduce exactly.
FIXTURE_WINDOW_START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleSource(Frozen):
    kind: Literal["lab", "fixture", "r3"]
    lab_project: str | None = None
    source_revision: str | None = None
    fixture_id: str | None = None
    variant: str | None = None


class BundleWindow(Frozen):
    start: datetime
    end: datetime
    source: str


class BundleScope(Frozen):
    incident_id: UUID
    run_id: UUID
    workload_id: UUID
    cluster: str
    namespace: str
    deployment: str
    uid: str
    window: BundleWindow
    request_text: str
    request_source: str


class BundleSlot(Frozen):
    slot: str
    evidence_id: UUID | None
    coverage_status: str
    coverage_reason_codes: list[str] = Field(default_factory=list)
    content_status: str
    time_relation: str
    payload_kind: str | None
    payload_version: int | None
    payload: dict | None
    sha256: str | None


class RawEvidenceBundle(Frozen):
    schema_version: Literal["RawEvidenceBundle/v1"] = BUNDLE_VERSION
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    captured_at: datetime
    source: BundleSource
    scope: BundleScope
    slots: list[BundleSlot]
    # Fixture bundles list only their present slots (the E06 fixture path); lab bundles
    # list every catalog slot, like `load_materials`.
    complete_catalog: bool
    reset_receipt_sha256: str | None = None
    manifest_sha256: str

    def body(self) -> dict:
        return self.model_dump(mode="json", exclude={"manifest_sha256"})


def seal(value: dict) -> RawEvidenceBundle:
    """Compute the manifest digest over the canonical body and validate the bundle."""
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    draft = RawEvidenceBundle.model_validate({**body, "manifest_sha256": "0" * 64})
    return draft.model_copy(update={"manifest_sha256": sha256_json(draft.body())})


def verify(bundle: RawEvidenceBundle) -> None:
    if sha256_json(bundle.body()) != bundle.manifest_sha256:
        raise ValueError(f"bundle manifest mismatch: {bundle.case_id}")


def read(path: Path) -> RawEvidenceBundle:
    bundle = RawEvidenceBundle.model_validate_json(Path(path).read_bytes())
    verify(bundle)
    return bundle


def write(path: Path, bundle: RawEvidenceBundle) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(bundle.model_dump(mode="json")) + "\n", encoding="utf-8")


def scope(bundle: RawEvidenceBundle) -> ContextScope:
    value = bundle.scope
    return ContextScope(
        knowledge=KnowledgeRunScope(
            incident_id=value.incident_id,
            run_id=value.run_id,
            workload_id=value.workload_id,
            cluster=value.cluster,
            namespace=value.namespace,
            name=value.deployment,
            workload_uid=value.uid,
            window_start=value.window.start,
            window_end=value.window.end,
        ),
        request_text=value.request_text,
        request_source=value.request_source,
        window_source=value.window.source,
    )


def _missing(slot: str) -> SlotMaterial:
    return SlotMaterial(
        slot=slot,
        source_class=SOURCE_CLASSES[slot],
        evidence_id=None,
        evidence_sha256=None,
        payload_version=None,
        coverage_status="missing",
        coverage_reason_codes=(),
        content_status="missing",
        time_relation="not_captured",
        entries=(),
        ambiguous_omitted=0,
    )


def _material(item: BundleSlot) -> SlotMaterial:
    """Mirror `context.load_materials` for one stored row."""
    entries = ()
    ambiguous = 0
    unsupported = False
    if item.content_status == "available":
        if item.payload is None or item.payload_kind is None or item.payload_version is None:
            unsupported = True
        else:
            try:
                projected = project(item.payload_kind, item.payload_version, item.payload)
            except UnsupportedProjection:
                unsupported = True
            else:
                ambiguous = sum(1 for entry in projected if entry.ambiguous)
                entries = tuple(entry for entry in projected if not entry.ambiguous)
    return SlotMaterial(
        slot=item.slot,
        source_class=SOURCE_CLASSES[item.slot],
        evidence_id=item.evidence_id,
        evidence_sha256=item.sha256,
        payload_version=item.payload_version,
        coverage_status=item.coverage_status,
        coverage_reason_codes=tuple(item.coverage_reason_codes),
        content_status=item.content_status,
        time_relation=item.time_relation,
        entries=entries,
        ambiguous_omitted=ambiguous,
        projection_unsupported=unsupported,
    )


def materials(bundle: RawEvidenceBundle) -> list[SlotMaterial]:
    by_slot = {item.slot: item for item in bundle.slots}
    if not bundle.complete_catalog:
        return [_material(item) for item in bundle.slots if item.slot in SOURCE_CLASSES]
    return [_material(by_slot[slot]) if slot in by_slot else _missing(slot) for slot in SLOT_CAPS]


def payloads(bundle: RawEvidenceBundle) -> dict[str, dict]:
    """Stored payloads of slots whose content is available (signal extraction input)."""
    return {
        item.slot: item.payload
        for item in bundle.slots
        if item.content_status == "available" and item.payload is not None
    }


# --- E06 fixture conversion ------------------------------------------------------------


def _fixture_kind(slot: str) -> str:
    if slot == "kubernetes.state":
        return "state"
    if slot == "kubernetes.events":
        return "events"
    if slot.startswith("kubernetes.logs."):
        return "logs"
    return "series"


def _fixture_payload(slot: str, payload: dict) -> dict:
    """Apply the eval_fixtures sample timestamp rewrite (60 s steps from the window start)."""
    fixed = copy.deepcopy(payload)
    if slot.startswith("prometheus."):
        start = int(FIXTURE_WINDOW_START.timestamp())
        for series in fixed.get("series", []):
            for index, sample in enumerate(series.get("samples", series.get("values", []))):
                if isinstance(sample, list) and len(sample) == 2:
                    sample[0] = start + index * 60
                elif isinstance(sample, dict):
                    sample["timestamp"] = start + index * 60
    return fixed


def fixture_bundle(case: dict) -> RawEvidenceBundle:
    case_id = case["id"]
    slots = []
    for slot, payload in case["evidence"].items():
        if slot not in SOURCE_CLASSES:
            continue
        fixed = _fixture_payload(slot, payload)
        slots.append(
            {
                "slot": slot,
                "evidence_id": str(uuid5(NAMESPACE_URL, f"{case_id}/{slot}/evidence")),
                "coverage_status": "available",
                "coverage_reason_codes": [],
                "content_status": "available",
                "time_relation": "within_requested_window",
                "payload_kind": _fixture_kind(slot),
                "payload_version": 1,
                "payload": fixed,
                "sha256": sha256_json(fixed),
            }
        )
    return seal(
        {
            "case_id": case_id,
            "captured_at": FIXTURE_WINDOW_START.isoformat(),
            "source": {"kind": "fixture", "fixture_id": f"e06/{case_id}"},
            "scope": {
                "incident_id": str(uuid5(NAMESPACE_URL, f"{case_id}/incident")),
                "run_id": str(uuid5(NAMESPACE_URL, f"{case_id}/run")),
                "workload_id": str(uuid5(NAMESPACE_URL, f"{case_id}/workload")),
                "cluster": "e10-controlled-cluster",
                "namespace": "e10-controlled",
                "deployment": "checkout",
                "uid": "uid-e10-controlled",
                "window": {
                    "start": FIXTURE_WINDOW_START.isoformat(),
                    "end": (FIXTURE_WINDOW_START + timedelta(minutes=30)).isoformat(),
                    "source": "explicit",
                },
                "request_text": case["question"],
                "request_source": "operator",
            },
            "slots": slots,
            "complete_catalog": False,
        }
    )


def fixture_cases() -> list[dict]:
    return json.loads((FIXTURE_ROOT / "cases.json").read_text(encoding="utf-8"))


def fixture_oracle() -> dict:
    return json.loads((FIXTURE_ROOT / "oracle.json").read_text(encoding="utf-8"))["cases"]
