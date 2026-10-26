import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl


def browser_origin(origin: str, secure: bool) -> str:
    """Validate one browser-serialized origin without silently rewriting configuration."""
    error = (
        "WEB_ORIGIN must be one exact browser origin, e.g. https://cloudops.example.com "
        "or http://localhost:5173; omit credentials, path, query, fragment and default port."
    )
    if (
        not origin.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in origin)
        or any(char in origin for char in "\\?#%@")
    ):
        raise RuntimeError(error)
    try:
        parsed = urlsplit(origin)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in {"http", "https"} or not host or parsed.path:
            raise ValueError
        if ":" not in host:
            labels = host.removesuffix(".").split(".")
            if len(host.removesuffix(".")) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
            ):
                raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        # The URL serializer handles IDNA, IPv4/IPv6 and default ports. Equality
        # rejects alternate spellings that a browser would send differently.
        if str(AnyHttpUrl(origin)) != origin + "/":
            raise ValueError
    except ValueError:
        raise RuntimeError(error) from None

    loopback_http = parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}
    if (loopback_http and secure) or (
        not loopback_http and (parsed.scheme != "https" or not secure)
    ):
        raise RuntimeError(
            "Use COOKIE_SECURE=false only with HTTP localhost/127.0.0.1 development; "
            "all other origins require HTTPS and COOKIE_SECURE=true."
        )
    return origin


def cookie_secure(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise RuntimeError("COOKIE_SECURE must be true or false.")
    return value.lower() == "true"


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
    secure = cookie_secure(os.environ.get("COOKIE_SECURE", "true"))
    origin = browser_origin(os.environ.get("WEB_ORIGIN", "http://localhost:5173"), secure)
    try:
        seconds = int(os.environ.get("SESSION_SECONDS", "28800"))
    except ValueError:
        raise RuntimeError("SESSION_SECONDS must be an integer from 1 to 28800.") from None
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
