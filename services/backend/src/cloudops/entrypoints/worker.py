import time

from sqlalchemy.dialects.postgresql import insert

from cloudops.shared.database import WorkerHeartbeat, database_now, require_schema, sessions


def heartbeat():
    with sessions()() as db, db.begin():
        now = database_now(db)
        db.execute(
            insert(WorkerHeartbeat)
            .values(name="collector", observed_at=now)
            .on_conflict_do_update(index_elements=[WorkerHeartbeat.name], set_={"observed_at": now})
        )


def main():
    require_schema()
    while True:
        heartbeat()
        time.sleep(1)


if __name__ == "__main__":
    main()
