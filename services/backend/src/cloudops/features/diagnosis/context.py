"""Server-scoped run context: persisted scope, slot materials and family eligibility.

Read-only: this module never commits, mutates a Run/Evidence row or opens a socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.versions import (
    ALIAS_CAPACITY,
    FAMILY_REQUIRED,
    SLOT_CAPS,
    SOURCE_CLASSES,
)
from cloudops.features.investigations.citation_schemas import Locator
from cloudops.features.investigations.locators import (
    Entry,
    UnsupportedProjection,
    canonical,
    digest as entry_digest,
)
from cloudops.features.investigations.locators import project as project_payload
from cloudops.features.investigations.locators import timestamp as normalize_timestamp
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.storage import evidence_views
from cloudops.features.knowledge.guidance import run_scope
from cloudops.features.knowledge.guidance_schemas import KnowledgeRunScope

LOG_SLOTS = frozenset({"kubernetes.logs.current", "kubernetes.logs.previous"})
REQUEST_SOURCES = frozenset({"operator", "title_default", "automatic"})


@dataclass(frozen=True)
class ContextScope:
    knowledge: KnowledgeRunScope
    request_text: str
    request_source: str
    window_source: str


@dataclass(frozen=True)
class SlotMaterial:
    slot: str
    source_class: str
    evidence_id: UUID | None
    evidence_sha256: str | None
    payload_version: int | None
    coverage_status: str
    coverage_reason_codes: tuple[str, ...]
    content_status: str
    time_relation: str
    entries: tuple[Entry, ...]
    ambiguous_omitted: int
    projection_unsupported: bool = False


@dataclass(frozen=True)
class RequiredSlot:
    slot: str
    status: str
    usable: bool
    waived: bool
    targets: int


@dataclass(frozen=True)
class FamilyAssessment:
    family: str
    eligible_for_assessment: bool
    reasons: tuple[str, ...]
    required_slots: tuple[RequiredSlot, ...]
    partial: bool


def resolve_scope(db, incident_id: UUID, run_id: UUID) -> ContextScope | None:
    knowledge = run_scope(db, incident_id, run_id)
    if knowledge is None:
        return None
    run = db.get(InvestigationRun, run_id)
    if run is None or run.question is None or not run.question.strip():
        return None
    request_source = run.question_source or "not_captured"
    if request_source not in REQUEST_SOURCES:
        return None
    return ContextScope(
        knowledge=knowledge,
        request_text=run.question,
        request_source=request_source,
        window_source=run.window_source,
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


def load_materials(db, scope: ContextScope) -> list[SlotMaterial]:
    rows = list(db.scalars(select(Evidence).where(Evidence.run_id == scope.knowledge.run_id)).all())
    views = evidence_views(db, rows)
    by_key = {view.source_key: (row, view) for row, view in zip(rows, views, strict=True)}
    materials = []
    for slot in SLOT_CAPS:
        found = by_key.get(slot)
        if found is None:
            materials.append(_missing(slot))
            continue
        row, view = found
        entries: tuple[Entry, ...] = ()
        ambiguous_omitted = 0
        unsupported = False
        if view.content_status == "available":
            if row.payload is None:
                unsupported = True
            else:
                try:
                    projected = project_payload(
                        view.payload_kind, view.payload_version, row.payload
                    )
                except UnsupportedProjection:
                    unsupported = True
                else:
                    ambiguous_omitted = sum(1 for entry in projected if entry.ambiguous)
                    entries = tuple(entry for entry in projected if not entry.ambiguous)
        materials.append(
            SlotMaterial(
                slot=slot,
                source_class=SOURCE_CLASSES[slot],
                evidence_id=view.id,
                evidence_sha256=view.sha256,
                payload_version=view.payload_version,
                coverage_status=view.coverage.status,
                coverage_reason_codes=tuple(view.coverage.reason_codes),
                content_status=view.content_status,
                time_relation=view.time_relation,
                entries=entries,
                ambiguous_omitted=ambiguous_omitted,
                projection_unsupported=unsupported,
            )
        )
    return materials


def usability(material: SlotMaterial, policy: EgressPolicy) -> tuple[bool, bool, list[str]]:
    """Return (usable, waived, closed reason codes) for one required slot."""
    if material.slot == "development_fixture":
        return False, False, ["synthetic_fixture_not_production_evidence"]
    if material.coverage_status == "not_applicable":
        if material.slot in LOG_SLOTS:
            return True, True, ["source_not_applicable"]
        return False, False, ["source_not_applicable"]
    if material.coverage_status == "missing":
        return False, False, ["source_missing"]
    if material.coverage_status == "unavailable":
        return False, False, ["required_slot_unavailable", *material.coverage_reason_codes]
    if material.content_status == "expired":
        return False, False, ["content_expired"]
    if material.content_status == "unavailable":
        return False, False, ["content_unavailable"]
    if material.projection_unsupported:
        return False, False, ["projection_unsupported"]
    if not policy.allows(material.source_class):
        return False, False, ["policy_denied"]
    selectable = _selectable(material, policy)
    if not selectable:
        if material.entries:
            return False, False, ["content_denied"]
        return False, False, ["no_selectable_entries"]
    reasons = ["source_partial"] if material.coverage_status == "partial" else []
    return True, False, reasons


def _selectable(material: SlotMaterial, policy: EgressPolicy) -> list[Entry]:
    return [entry for entry in material.entries if policy.scan(canonical(entry.value)) is None]


def family_matrix(materials: list[SlotMaterial], policy: EgressPolicy) -> list[FamilyAssessment]:
    by_slot = {material.slot: material for material in materials}
    fixture_present = any(
        material.slot == "development_fixture" and material.entries for material in materials
    )
    assessments = []
    for family, slots in FAMILY_REQUIRED.items():
        required_slots = []
        reasons: list[str] = []
        eligible = True
        partial = False
        target_total = 0
        for slot in slots:
            material = by_slot.get(slot) or _missing(slot)
            usable, waived, slot_reasons = usability(material, policy)
            if waived:
                status = "waived"
            elif usable:
                status = material.coverage_status
                target_total += len(_selectable(material, policy))
                if material.coverage_status == "partial":
                    partial = True
            else:
                status = slot_reasons[0] if slot_reasons else "unusable"
                eligible = False
                reasons.append("required_slot_unavailable")
            required_slots.append(
                RequiredSlot(
                    slot=slot,
                    status=status,
                    usable=usable,
                    waived=waived,
                    targets=len(_selectable(material, policy)) if usable else 0,
                )
            )
        if eligible and target_total == 0:
            eligible = False
            reasons.append("no_required_targets")
        if not eligible and fixture_present:
            reasons.append("synthetic_fixture_not_production_evidence")
        assessments.append(
            FamilyAssessment(
                family=family,
                eligible_for_assessment=eligible,
                reasons=tuple(dict.fromkeys(reasons)),
                required_slots=tuple(required_slots),
                partial=partial,
            )
        )
    return assessments


@dataclass(frozen=True)
class Extracted:
    slot: str
    entries: tuple[Entry, ...]
    rule_omitted: int
    ambiguous_omitted: int


class InsufficientContextError(ValueError):
    """A build that cannot produce a safe required context; reason is a closed code."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ContextTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alias: str = Field(pattern=r"^E\d{3}$")
    evidence_id: UUID
    source_key: str
    source_class: str
    locator: Locator
    label: str = Field(min_length=1, max_length=120)
    value: Any
    entry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str
    time_relation: str
    coverage_status: str
    value_bytes: int = Field(ge=1)
    family_slots: list[str] = Field(default_factory=list)


def _clean(material: SlotMaterial) -> tuple[list[Entry], int]:
    kept = [entry for entry in material.entries if not entry.ambiguous]
    ambiguous = material.ambiguous_omitted + (len(material.entries) - len(kept))
    return kept, ambiguous


def _value_stamp(entry: Entry) -> Decimal | None:
    if isinstance(entry.value, dict):
        for key in ("lastTimestamp", "eventTime", "creationTimestamp"):
            if key in entry.value:
                stamp = normalize_timestamp(entry.value[key])
                if stamp is not None:
                    return Decimal(stamp)
    stamp = normalize_timestamp(entry.value)
    return Decimal(stamp) if stamp is not None else None


def _newest_key(entry: Entry) -> tuple:
    stamp = _value_stamp(entry)
    return (stamp is None, -stamp if stamp is not None else Decimal(0), entry.key)


def _log_identity(entry: Entry) -> tuple[str, Decimal | None]:
    try:
        parts = json.loads(entry.identity)
    except (TypeError, ValueError):
        return entry.key, None
    if (
        isinstance(parts, list)
        and len(parts) == 5
        and isinstance(parts[0], str)
        and isinstance(parts[1], str)
    ):
        stamp = normalize_timestamp(parts[3])
        return canonical([parts[0], parts[1], parts[2]]), (
            Decimal(stamp) if stamp is not None else None
        )
    return entry.key, None


def _log_key(entry: Entry) -> tuple:
    _, stamp = _log_identity(entry)
    return (stamp is None, -stamp if stamp is not None else Decimal(0), entry.key)


def _select(entries: list[Entry], order: str, cap: int) -> tuple[list[Entry], int]:
    if cap <= 0:
        return [], len(entries)
    if order == "group_newest":
        groups: dict[str, list[Entry]] = {}
        for entry in entries:
            group, _ = _log_identity(entry)
            groups.setdefault(group, []).append(entry)
        kept: list[Entry] = []
        omitted = 0
        for group in sorted(groups):
            ordered = sorted(groups[group], key=_log_key)
            kept.extend(ordered[:cap])
            omitted += max(0, len(ordered) - cap)
        return kept, omitted
    if order == "newest_first":
        ordered = sorted(entries, key=_newest_key)
    else:
        ordered = sorted(entries, key=lambda entry: entry.key)
    return ordered[:cap], max(0, len(ordered) - cap)


def _extract(material: SlotMaterial, *, required: bool) -> Extracted:
    entries, ambiguous = _clean(material)
    required_cap, optional_cap, order = SLOT_CAPS[material.slot]
    if required and material.slot == "development_fixture":
        return Extracted(material.slot, (), 0, ambiguous)
    selected, omitted = _select(entries, order, required_cap if required else optional_cap)
    return Extracted(material.slot, tuple(selected), omitted, ambiguous)


def extract_required(material: SlotMaterial) -> Extracted:
    return _extract(material, required=True)


def extract_optional(material: SlotMaterial) -> Extracted:
    return _extract(material, required=False)


def _target(alias: str, material: SlotMaterial, entry: Entry, families: list[str]) -> ContextTarget:
    label = (entry.label or "Stored item")[:120]
    return ContextTarget(
        alias=alias,
        evidence_id=material.evidence_id,
        source_key=material.slot,
        source_class=material.source_class,
        locator=Locator(kind=entry.kind, key=entry.key),
        label=label,
        value=entry.value,
        entry_digest=entry_digest(entry.value),
        evidence_sha256=material.evidence_sha256 or "",
        time_relation=material.time_relation,
        coverage_status=material.coverage_status,
        value_bytes=len(canonical(entry.value).encode("utf-8")),
        family_slots=families,
    )


def required_targets(
    materials: list[SlotMaterial], families: list[FamilyAssessment]
) -> list[ContextTarget]:
    by_slot = {material.slot: material for material in materials}
    chosen: dict[tuple, tuple[SlotMaterial, Entry, list[str]]] = {}
    for family in families:
        if not family.eligible_for_assessment:
            continue
        for slot in FAMILY_REQUIRED[family.family]:
            material = by_slot.get(slot)
            if material is None:
                continue
            for entry in extract_required(material).entries:
                key = (material.evidence_id, entry.kind, entry.key)
                record = chosen.get(key)
                if record is None:
                    chosen[key] = (material, entry, [family.family])
                elif family.family not in record[2]:
                    record[2].append(family.family)
    targets = []
    for index, (material, entry, slots) in enumerate(chosen.values(), start=1):
        if index > ALIAS_CAPACITY:
            raise InsufficientContextError("required_alias_space_exceeded")
        targets.append(_target(f"E{index:03d}", material, entry, slots))
    return targets


def optional_targets(
    materials: list[SlotMaterial],
    families: list[FamilyAssessment],
    used: set[tuple],
) -> list[ContextTarget]:
    required_slots: set[str] = set()
    for family in families:
        if family.eligible_for_assessment:
            required_slots.update(FAMILY_REQUIRED[family.family])
    by_slot = {material.slot: material for material in materials}
    targets = []
    index = len(used)
    for slot in SLOT_CAPS:
        if slot in required_slots:
            continue
        material = by_slot.get(slot)
        if material is None:
            continue
        for entry in extract_optional(material).entries:
            key = (material.evidence_id, entry.kind, entry.key)
            if key in used:
                continue
            index += 1
            if index > ALIAS_CAPACITY:
                return targets
            targets.append(_target(f"E{index:03d}", material, entry, []))
    return targets
