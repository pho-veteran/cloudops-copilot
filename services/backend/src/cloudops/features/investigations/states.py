"""E04-owned investigation state predicates shared by admission, recovery and queue consumers."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

# Future E04 producers (analyzing, waiting_for_quota) are listed so consumers fail closed today.
NONTERMINAL_RUN = ("queued", "collecting", "analyzing", "waiting_for_quota")
TERMINAL_RUN = ("insufficient_evidence", "completed", "failed", "cancelled")
COLLECTION_JOB = "development_collection_v1"


def nonterminal_run(db: Session, incident_id: UUID):
    """Return a Run that is not known to be terminal; an unrecognized state also blocks."""
    from cloudops.features.investigations.models import InvestigationRun

    return db.scalar(
        select(InvestigationRun.id)
        .where(
            InvestigationRun.incident_id == incident_id,
            InvestigationRun.state.not_in(TERMINAL_RUN),
        )
        .limit(1)
    )
