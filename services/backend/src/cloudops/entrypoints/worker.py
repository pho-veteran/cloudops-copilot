import logging
import os
import threading
import time

from sqlalchemy.dialects.postgresql import insert

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.features.diagnosis.lifecycle import configure_dispatch
from cloudops.entrypoints.dispatch import GATE, PERMIT_CLASSES
from cloudops.features.investigations.admission_settings import deferred_run_limit
from cloudops.features.investigations.collector import (
    CollectionHandler,
    Interrupted,
    collect_checkpointed,
    configured_adapters,
    prepare,
)
from cloudops.features.investigations.promotion import promote_deferred
from cloudops.features.recommendations.worker import ReportHandler, generate
from cloudops.features.recovery.service import ExecutionHandler, execute
from cloudops.features.recovery.outcomes import RECONCILE_JOB
from cloudops.features.recovery.reconciliation import ReconciliationHandler, reconcile
from cloudops.features.recovery.verification import VerificationHandler, verify
from cloudops.features.recovery.verification_policy import VERIFICATION_JOB
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, require_schema, sessions
from cloudops.shared.jobs import Claim, claim_next, finish, renew, start_call

from cloudops.features.diagnosis.lifecycle import admit, ANALYSIS_JOB, PROBE_JOB
from cloudops.features.diagnosis.worker import (
    AnalysisHandler,
    ProbeHandler,
    analyze,
    probe,
    expire_waits,
)
from cloudops.features.diagnosis.slot import ModelSlot, GuardBusy

configure_dispatch(GATE)

HANDLERS = {
    ANALYSIS_JOB: AnalysisHandler(),
    PROBE_JOB: ProbeHandler(),
    "development_collection_v1": CollectionHandler(finalize=admit),
    "recommendation_report_v1": ReportHandler(),
    "recovery_execution_v1": ExecutionHandler(),
    RECONCILE_JOB: ReconciliationHandler(),
    VERIFICATION_JOB: VerificationHandler(),
}
if HANDLERS.keys() != PERMIT_CLASSES.keys():
    raise RuntimeError("Every worker job kind needs exactly one dispatch permit class.")
logger = logging.getLogger(__name__)
_last_denial: list[str | None] = [None]


def heartbeat():
    with sessions()() as db, db.begin():
        now = database_now(db)
        db.execute(
            insert(WorkerHeartbeat)
            .values(name="collector", observed_at=now)
            .on_conflict_do_update(index_elements=[WorkerHeartbeat.name], set_={"observed_at": now})
        )


def _report_denial():
    _, kinds, reason = GATE.current()
    reason = reason if not kinds else None
    if reason != _last_denial[0]:
        _last_denial[0] = reason
        if reason:
            logger.warning(
                "Job dispatch denied by trusted authority (%s); nothing is claimed.", reason
            )
        else:
            logger.info("Job dispatch permit accepted.")


def _require_dispatch(claim: Claim, lease_lost: threading.Event) -> None:
    """Admit at the feature's actual call boundary, after request preparation."""
    if lease_lost.is_set() or not start_call(claim, GATE):
        raise Interrupted()


def run_once() -> bool:
    heartbeat()
    _report_denial()
    # Deferred investigations take free dispatch slots before the next claim.
    promote_deferred(gate=GATE)
    expire_waits()
    slot = ModelSlot()
    model_handlers = {
        key: value for key, value in HANDLERS.items() if key in {ANALYSIS_JOB, PROBE_JOB}
    }
    try:
        slot.__enter__()
    except GuardBusy:
        slot = None
    try:
        # Preserve the common FIFO queue. Owning model capacity does not give model
        # jobs priority over an older collection/recommendation/recovery request.
        eligible_handlers = (
            HANDLERS
            if slot is not None
            else {key: value for key, value in HANDLERS.items() if key not in model_handlers}
        )
        claim = claim_next(eligible_handlers, GATE)
    except BaseException:
        if slot is not None:
            slot.release()
        raise
    if slot is not None and (claim is None or claim.kind not in model_handlers):
        slot.release()
        slot = None
    if not claim:
        return False
    stopped = threading.Event()
    lease_lost = threading.Event()

    def keep_lease():
        while not stopped.wait(settings().heartbeat_seconds):
            try:
                heartbeat()
                if not renew(claim):
                    lease_lost.set()
                    return
            except Exception:
                lease_lost.set()
                logger.warning("Worker lease renewal unavailable; expired output will be rejected.")
                return

    keeper = threading.Thread(target=keep_lease, daemon=True)
    keeper.start()
    try:
        if claim.kind in {ANALYSIS_JOB, PROBE_JOB}:
            (analyze if claim.kind == ANALYSIS_JOB else probe)(claim, GATE, slot, lease_lost)
            return True
        if claim.kind == RECONCILE_JOB:
            reconcile(claim, GATE, lease_lost)
            return True
        if claim.kind == VERIFICATION_JOB:
            verify(claim, GATE, lease_lost)
            return True
        if claim.kind == "development_collection_v1":
            adapters = configured_adapters(os.environ.get("COLLECTION_SOURCE_MODE", "none"))
            context = prepare(
                claim.subject_id, attempt_number=claim.attempt, max_attempts=claim.max_attempts
            )
            result = collect_checkpointed(claim, context, adapters, GATE, lease_lost)
        elif claim.kind == "recommendation_report_v1":
            result = generate(
                claim.subject_id, before_call=lambda: _require_dispatch(claim, lease_lost)
            )
        elif claim.kind == "recovery_execution_v1":
            result = execute(
                claim.subject_id, before_call=lambda: _require_dispatch(claim, lease_lost)
            )
        else:
            raise RuntimeError("Unknown job kind; dispatch fails closed.")
        if not finish(claim, HANDLERS[claim.kind], result=result, gate=GATE):
            logger.info("Obsolete job result rejected.")
    except Interrupted:
        # No publication under a lost lease or retired permit generation; the lease expires.
        logger.info("Job attempt stopped at a dispatch or lease boundary.")
    except Exception:
        if claim.kind in {ANALYSIS_JOB, PROBE_JOB}:
            logger.warning("Model attempt interrupted; its durable reservation governs recovery.")
            return True
        if not finish(
            claim,
            HANDLERS[claim.kind],
            error="Bounded job attempt failed; inspect its recorded feature outcome.",
            gate=GATE,
        ):
            logger.info("Obsolete job failure rejected; its lease will expire.")
    finally:
        stopped.set()
        keeper.join(timeout=2)
        if slot is not None:
            slot.release()
    return True


def main():
    require_schema()
    configured_adapters(os.environ.get("COLLECTION_SOURCE_MODE", "none"))
    deferred_run_limit()
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.error(
                "Worker cycle failed (%s). Check database/migrations; retrying in 5 seconds.",
                type(exc).__name__,
            )
            time.sleep(5)
        time.sleep(1)


if __name__ == "__main__":
    main()
