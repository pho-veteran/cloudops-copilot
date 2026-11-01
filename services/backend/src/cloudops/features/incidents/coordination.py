"""Admission coordination shared by features that create or dispatch bounded jobs.

Admission/dispatch lock order (their live preflight runs before these locks):

- Admission: admission lock -> Incident (attach) -> Workload -> insert Run/Job.
- Promotion: admission lock -> deferred Run (SKIP LOCKED); Incident/Workload are read only.
- Cancel/resolve: Incident -> nonterminal Runs -> their queued Job (SKIP LOCKED) -> plans.
- Claim/reclaim: Job (SKIP LOCKED, shared mechanics) -> Run -> plans; other rows read only.
- Approval: admission lock -> Incident -> Plan -> Review -> Workload; Runs are read only.

Cancellation never waits for a Job lock, so it cannot form a cycle with a claimer; the
claimer observes the terminal Run afterwards and retires the Job without an attempt.

Legacy recovery proposal/read probes can still hold Incident/Workload row locks across
bounded live reads. An attach waiting for one of those rows can indirectly retain the
admission lock; this existing contention remains an E07 concern, not a lock-free claim.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

ADMISSION_LOCK = 742001


def lock_admission(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK})
