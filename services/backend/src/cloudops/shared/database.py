from datetime import datetime
from functools import lru_cache
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from cloudops.shared.config import settings

BACKEND_ROOT = Path(__file__).resolve().parents[3]


class Base(DeclarativeBase):
    pass


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True)


def sessions():
    return sessionmaker(engine(), expire_on_commit=False)


def get_db():
    with sessions()() as db:
        yield db


def require_schema():
    scripts = ScriptDirectory(str(BACKEND_ROOT / "migrations"))
    with engine().connect() as connection:
        current = MigrationContext.configure(connection).get_current_heads()
    if set(current) != set(scripts.get_heads()):
        raise RuntimeError(
            "Database schema is not current. Run the documented migrate command before startup."
        )


def database_now(db: Session) -> datetime:
    return db.scalar(func.clock_timestamp())
