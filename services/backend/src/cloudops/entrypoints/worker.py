import logging
import threading
import time

from sqlalchemy.dialects.postgresql import insert

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.features.investigations.collector import CollectionHandler, collect, prepare
from cloudops.features.recommendations.worker import ReportHandler, generate
from cloudops.features.recovery.service import ExecutionHandler, execute
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, require_schema, sessions
from cloudops.shared.jobs import claim_next, finish, renew

HANDLERS = {
    "development_collection_v1": CollectionHandler(),
    "recommendation_report_v1": ReportHandler(),
    "recovery_execution_v1": ExecutionHandler(),
}
logger = logging.getLogger(__name__)


def heartbeat():
    with sessions()() as db, db.begin():
        now = database_now(db)
        db.execute(
            insert(WorkerHeartbeat)
            .values(name="collector", observed_at=now)
            .on_conflict_do_update(index_elements=[WorkerHeartbeat.name], set_={"observed_at": now})
        )


def run_once() -> bool:
    heartbeat()
    claim = claim_next(HANDLERS)
    if not claim:
        return False
    stopped = threading.Event()

    def keep_lease():
        while not stopped.wait(settings().heartbeat_seconds):
            try:
                heartbeat()
                if not renew(claim):
                    return
            except Exception:
                logger.warning("Worker lease renewal unavailable; expired output will be rejected.")
                return

    keeper = threading.Thread(target=keep_lease, daemon=True)
    keeper.start()
    try:
        if claim.kind == "development_collection_v1":
            result = collect(prepare(claim.subject_id))
        elif claim.kind == "recommendation_report_v1":
            result = generate(claim.subject_id)
        else:
            result = execute(claim.subject_id)
        if not finish(claim, HANDLERS[claim.kind], result=result):
            logger.info("Obsolete job result rejected.")
    except Exception:
        finish(
            claim,
            HANDLERS[claim.kind],
            error="Bounded job attempt failed; inspect its recorded feature outcome.",
        )
    finally:
        stopped.set()
        keeper.join(timeout=2)
    return True


def main():
    require_schema()
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
