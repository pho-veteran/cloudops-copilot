"""Dedicated read-only polling process. No product jobs or executor credentials."""

import asyncio
import logging
import signal

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from cloudops.features.workloads.observation_service import (
    database_call,
    due,
    observe_direct,
    sweep,
)
from cloudops.features.workloads.observation_settings import POLICY
from cloudops.shared.config import settings
from cloudops.shared.database import engine as startup_engine, require_schema

log = logging.getLogger(__name__)


async def run(stop):
    require_schema()
    startup_engine().dispose()
    engine = create_engine(
        settings().database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    tasks = set()
    ticks = 0
    try:
        while not stop.is_set():
            finished = {task for task in tasks if task.done()}
            for task in finished:
                try:
                    task.result()
                except SQLAlchemyError:
                    log.warning(
                        "Observation database unavailable; retrying within scheduler bounds."
                    )
                except Exception:
                    # Never log provider exceptions or malformed source values.
                    log.error("Observation attempt failed; lease expiry will fence publication.")
            tasks -= finished
            try:
                if ticks % 30 == 0:
                    await database_call(sweep, factory)
                if len(tasks) < POLICY.scheduled_slots:
                    for workload_id in (await database_call(due, factory))[
                        : POLICY.scheduled_slots - len(tasks)
                    ]:
                        tasks.add(
                            asyncio.create_task(
                                observe_direct(workload_id, "scheduled", factory=factory)
                            )
                        )
            except SQLAlchemyError:
                log.warning("Observation database unavailable; scheduler is backing off.")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
            ticks += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()


def main():
    logging.basicConfig(level=logging.INFO)
    # Keep HTTP client request logging disabled: destinations remain host-private.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    asyncio.run(run(stop))


if __name__ == "__main__":
    main()
