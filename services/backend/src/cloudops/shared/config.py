import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    web_origin: str
    cookie_secure: bool
    session_seconds: int
    workloads_file: Path
    lease_seconds: int = 30
    heartbeat_seconds: int = 5
    worker_stale_seconds: int = 45
    queue_limit: int = 100
    collector_delay_seconds: float = 2
    collector_fail: bool = False


@lru_cache
def settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL psycopg URL; see .env.example.")
    origin = os.environ.get("WEB_ORIGIN", "http://localhost:5173")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise RuntimeError("WEB_ORIGIN must be an exact origin without a path.")
    secure = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
    if not secure and (
        parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
    ):
        raise RuntimeError(
            "Insecure cookies are allowed only for explicit loopback HTTP development."
        )
    seconds = int(os.environ.get("SESSION_SECONDS", "28800"))
    delay = float(os.environ.get("COLLECTOR_DELAY_SECONDS", "2"))
    if not 1 <= seconds <= 28800 or not 0 <= delay <= 300:
        raise RuntimeError(
            "SESSION_SECONDS must be 1..28800; COLLECTOR_DELAY_SECONDS must be 0..300."
        )
    return Settings(
        database_url=database_url,
        web_origin=origin,
        cookie_secure=secure,
        session_seconds=seconds,
        workloads_file=Path(os.environ.get("WORKLOADS_FILE", "config/development-workloads.json")),
        collector_delay_seconds=delay,
        collector_fail=os.environ.get("COLLECTOR_FAIL", "false").lower() == "true",
    )
