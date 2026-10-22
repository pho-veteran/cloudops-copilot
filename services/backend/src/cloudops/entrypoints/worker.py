import logging
import threading
import time

from sqlalchemy.dialects.postgresql import insert

from cloudops.features.investigations.collector import CollectionHandler, collect, prepare
from cloudops.shared.config import settings
from cloudops.shared.database import WorkerHeartbeat, database_now, require_schema, sessions
from cloudops.shared.jobs import claim_next, finish, renew

HANDLERS = {"development_collection_v1": CollectionHandler()}
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
        result = collect(prepare(claim.subject_id))
        if not finish(claim, HANDLERS[claim.kind], result=result):
            logger.info("Obsolete collection result rejected.")
    except Exception:
        finish(claim, HANDLERS[claim.kind], error="Development fixture collection failed.")
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
        except Exception:
            logger.error(
                "Worker database unavailable. Check database/migrations; retrying in 5 seconds."
            )
            time.sleep(5)
        time.sleep(1)


if __name__ == "__main__":
    main()
