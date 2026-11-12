"""Session-level PostgreSQL writer guard and safe cleanup for Knowledge activation."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from cloudops.shared.database import engine

# A dedicated feature namespace; this is not the incident or observation lock.
LOCK_KEY = 742059
_LOCK_HIGH = (LOCK_KEY >> 32) & 0xFFFFFFFF
_LOCK_LOW = LOCK_KEY & 0xFFFFFFFF
_LOCK_SQL = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(:key)")
_PID_SQL = text("SELECT pg_backend_pid()")
_HELD_SQL = text(
    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND granted "
    "AND classid = :high AND objid = :low AND objsubid = 1 LIMIT 1"
)
_HELD_BY_SESSION_SQL = text(
    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND granted "
    "AND classid = :high AND objid = :low AND objsubid = 1 AND pid = pg_backend_pid() LIMIT 1"
)


class GuardBusy(RuntimeError):
    """Another Knowledge writer already owns the common guard."""


class GuardLost(RuntimeError):
    """The writer can no longer prove it owns the original session lock."""


class WriterGuard:
    def __init__(self) -> None:
        self.connection: Connection | None = None
        self.pid: int | None = None

    def __enter__(self) -> "WriterGuard":
        connection = engine().connect()
        acquired = False
        try:
            acquired = bool(connection.execute(_LOCK_SQL, {"key": LOCK_KEY}).scalar())
            connection.commit()
            if not acquired:
                connection.close()
                raise GuardBusy("Another Knowledge index writer is active.")
            self.connection = connection
            self.pid = int(connection.execute(_PID_SQL).scalar())
            connection.commit()
            return self
        except GuardBusy:
            raise
        except Exception:
            _safe_close(connection, acquired=acquired)
            raise

    @property
    def active(self) -> Connection:
        if self.connection is None:
            raise GuardLost("Knowledge writer guard is not active.")
        return self.connection

    def verify(self, *, commit: bool = True) -> None:
        connection = self.active
        try:
            pid = int(connection.execute(_PID_SQL).scalar())
            held = connection.execute(
                _HELD_BY_SESSION_SQL, {"high": _LOCK_HIGH, "low": _LOCK_LOW}
            ).first()
            if commit:
                connection.commit()
        except Exception as exc:
            raise GuardLost("Knowledge writer session is no longer available.") from exc
        if pid != self.pid or held is None:
            raise GuardLost("Knowledge writer guard is no longer owned by this session.")

    def release(self) -> None:
        connection = self.connection
        self.connection = None
        self.pid = None
        if connection is None:
            return
        try:
            unlocked = connection.execute(_UNLOCK_SQL, {"key": LOCK_KEY}).scalar()
            connection.commit()
            if not unlocked:
                connection.invalidate()
        except Exception:
            connection.invalidate()
        finally:
            connection.close()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _safe_close(connection: Connection, *, acquired: bool) -> None:
    try:
        if acquired:
            connection.execute(_UNLOCK_SQL, {"key": LOCK_KEY})
            connection.commit()
    except Exception:
        connection.invalidate()
    finally:
        connection.close()


def lock_present(db: Session | Connection) -> bool:
    try:
        if isinstance(db, Session):
            bind = db.get_bind()
            with bind.connect() as connection:
                row = connection.execute(_HELD_SQL, {"high": _LOCK_HIGH, "low": _LOCK_LOW}).first()
                connection.commit()
                return row is not None
        row = db.execute(_HELD_SQL, {"high": _LOCK_HIGH, "low": _LOCK_LOW}).first()
        db.commit()
        return row is not None
    except Exception:
        return False
