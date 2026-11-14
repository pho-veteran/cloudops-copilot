"""Trusted post-restore cleanup while external analysis dispatch remains denied."""

import json

from sqlalchemy import select, update

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.dispatch_admin import trusted_actor
from cloudops.features.access.service import revoke_all_sessions
from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, configure_dispatch
from cloudops.features.diagnosis.worker import expire_waits
from cloudops.features.diagnosis.models import Analysis, AnalysisContext
from cloudops.features.diagnosis.lifecycle import ACTIVE
from cloudops.shared.database import require_schema, sessions
from cloudops.shared.dispatch_permit import dispatch_root, PermitError
from cloudops.shared.jobs import Job


def main():
    root = dispatch_root()
    if root is None:
        raise PermitError("Mount the existing trusted dispatch authority.")
    trusted_actor(root, create=False)
    permit, kinds, reason = GATE.current()
    if permit is None or permit.mode != "recovery" or ANALYSIS_JOB in kinds:
        raise PermitError("Keep analysis denied in recovery before restored-work cleanup.")
    require_schema()
    configure_dispatch(GATE)
    with sessions()() as db:
        revoke_all_sessions(db)
    # Batches are bounded in the worker; maintenance drains all old epochs while workers are stopped.
    previous = None
    while True:
        expire_waits()
        with sessions()() as db:
            active = list(
                db.scalars(
                    select(Job).where(
                        Job.kind.in_(["diagnosis_analysis_v1", "diagnosis_probe_v1"]),
                        Job.state.in_(["queued", "held", "running"]),
                    )
                )
            )
        if not active:
            break
        if previous is not None and len(active) >= previous:
            raise RuntimeError(
                "Pending work remains: keep dispatch denied and inspect before resuming."
            )
        previous = len(active)
    with sessions()() as db, db.begin():
        # A restored terminal row never grants permanent retention of its working copy.
        db.execute(
            update(AnalysisContext)
            .where(
                AnalysisContext.analysis_id.in_(
                    select(Analysis.id).where(Analysis.state.not_in(ACTIVE))
                ),
                AnalysisContext.snapshot.is_not(None),
            )
            .values(snapshot=None)
        )
    print(
        json.dumps(
            {
                "sessions_revoked": True,
                "restored_model_work_retired": True,
                "analysis_dispatch_denied": True,
                "permit_generation": permit.generation,
            }
        )
    )


if __name__ == "__main__":
    main()
