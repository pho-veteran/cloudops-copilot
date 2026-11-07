import logging
import os
import threading
import time

from sqlalchemy.dialects.postgresql import insert

from cloudops.entrypoints import metadata  # noqa: F401
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
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, require_schema, sessions
from cloudops.shared.jobs import Claim, claim_next, finish, renew, start_call

HANDLERS = {
    "development_collection_v1": CollectionHandler(),
    "recommendation_report_v1": ReportHandler(),
    "recovery_execution_v1": ExecutionHandler(),
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
    claim = claim_next(HANDLERS, GATE)
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
