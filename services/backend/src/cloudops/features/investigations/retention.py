"""Trusted, transactional expiry of unpinned evidence content."""

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from cloudops.features.investigations.models import Evidence, EvidencePin
from cloudops.shared.database import database_now


def purge_expired(db: Session, *, apply: bool = False) -> int:
    now = database_now(db)
    # Lock in the same order as resolution pinning. Recheck pins after locking so a
    # resolution that won the race cannot lose its content.
    candidates = db.scalars(
        select(Evidence.id)
        .where(Evidence.expires_at <= now, Evidence.payload.is_not(None))
        .order_by(Evidence.id)
        .with_for_update()
    ).all()
    count = 0
    for evidence_id in candidates:
        if db.scalar(select(exists().where(EvidencePin.evidence_id == evidence_id))):
            continue
        count += 1
        if apply:
            db.execute(update(Evidence).where(Evidence.id == evidence_id).values(payload=None))
    if apply:
        db.commit()
    return count
