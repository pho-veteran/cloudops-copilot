"""Temporary analysis pins for the exact evidenced context of one analysis.

``acquire`` is atomic and caller-transactional: it locks every contributing Evidence
row in the same ascending ``Evidence.id`` order as ``retention.purge_expired``,
re-reads availability under the lock and inserts nothing when any contributor is no
longer available. It never resurrects expired content, never updates Evidence and
never commits (issue #69 §11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cloudops.features.diagnosis.context import PreparedContext
from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.features.investigations.storage import evidence_views


@dataclass(frozen=True)
class PinDenied:
    reason: Literal["contributing_content_unavailable"]
    slot: str
    alias: str | None


PinResult = Literal["acquired"] | PinDenied


def _contributing(context: PreparedContext) -> dict[UUID, object]:
    """Distinct contributing evidence ids, keeping the first citing target."""
    first: dict[UUID, object] = {}
    for target in (*context.required_targets, *context.optional_targets):
        first.setdefault(target.evidence_id, target)
    return first


def acquire(db: Session, analysis_id: UUID, context: PreparedContext) -> PinResult:
    """Atomically pin every contributing row, or deny without inserting anything."""
    contributing = _contributing(context)
    if not contributing:
        return "acquired"
    locked = db.scalars(
        select(Evidence)
        .where(Evidence.id.in_(contributing))
        .order_by(Evidence.id)
        .with_for_update(of=Evidence)
    ).all()
    views = evidence_views(db, list(locked))
    bad = {view.id for view in views if view.content_status != "available"}
    bad.update(set(contributing) - {row.id for row in locked})
    if bad:
        for target in (*context.required_targets, *context.optional_targets):
            if target.evidence_id in bad:
                return PinDenied(
                    reason="contributing_content_unavailable",
                    slot=target.source_key,
                    alias=target.alias,
                )
    ids = [row.id for row in locked]
    existing = set(
        db.scalars(
            select(EvidencePin.evidence_id).where(
                EvidencePin.kind == "analysis",
                EvidencePin.owner_id == analysis_id,
                EvidencePin.evidence_id.in_(ids),
            )
        ).all()
    )
    for evidence_id in ids:
        if evidence_id not in existing:
            db.add(
                EvidencePin(
                    evidence_id=evidence_id,
                    kind="analysis",
                    owner_id=analysis_id,
                )
            )
    db.flush()
    return "acquired"


def release(db: Session, analysis_id: UUID) -> int:
    """Delete only this analysis's pins; other owners and content are untouched."""
    result = db.execute(
        delete(EvidencePin).where(
            EvidencePin.kind == "analysis",
            EvidencePin.owner_id == analysis_id,
        )
    )
    return int(result.rowcount or 0)
