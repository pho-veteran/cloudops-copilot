"""Small measured HTTP workload; controls are deployment-owned and never HTTP inputs.

Every behaviour below is selected by the Deployment's environment, which only the lab
driver sets. With no ``LAB_*`` value the application behaves exactly as before: it
serves ``/work``, ``/health`` and ``/metrics`` and logs one startup line.

Engine-lab values (dataset v2):

- ``LAB_MODE``: ``healthy``; ``crash`` (exit at startup); ``oom`` (allocate past the
  limit at startup); ``slow`` (slow handler); ``crash_after`` (serve, then exit);
  ``setting`` (a required setting is missing at startup); ``leak`` (retain memory over
  time); ``hold`` (retain a fixed amount); ``errors`` (a fraction of 503 responses).
- Parameters: ``LAB_DELAY_MS``, ``LAB_SLOW_ERRORS`` (``0`` disables the slow 503s),
  ``LAB_SLOW_SHARE`` (the share of slowed requests),
  ``LAB_ERROR_FRACTION``, ``LAB_ALLOC_MIB``, ``LAB_LEAK_MIB_PER_S``, ``LAB_HOLD_MIB``,
  ``LAB_CRASH_AFTER_S``, ``LAB_SERVICE_MS``, ``LAB_WORK_MS``, ``LAB_READY_DELAY_S``.
- ``LAB_ONCE`` (``crash`` or ``oom``) with ``LAB_STATE_DIR``: fail once in a Pod, then
  serve normally after the restart (a marker file in the Pod's ``emptyDir``).
- ``LAB_IGNORE_TERM=1``: ignore SIGTERM, so a kubelet kill ends with SIGKILL.
- Logs: ``LAB_LOG_PROFILE`` (``plain``, ``json`` or ``kv``), ``LAB_REQUEST_LOG=1`` (a
  sampled per-request line and every error), ``LAB_WARN_EVERY_S`` (unrelated periodic
  warnings), ``LAB_SEED`` (template choice) and ``LAB_LOG_FIELD=1`` (log the request's
  ``note`` field, bounded and printable). The request field only changes what this
  application logs.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit

BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
LOCK = threading.Lock()
COUNTS = Counter()
TOTAL_SECONDS = 0.0
SLOTS = threading.BoundedSemaphore(2)
MODE = os.environ.get("LAB_MODE", "healthy")
STARTED = time.monotonic()
RETAINED = []
NOTE_EVERY_S = 5
LAST_NOTE = [0.0]
TICKETS = [0]


def _number(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    number = float(value)
    if number < 0 or number > 100000:
        raise SystemExit(f"{name} is out of range")
    return number


SEED = os.environ.get("LAB_SEED", "")
PROFILE = os.environ.get("LAB_LOG_PROFILE", "plain")
REQUEST_LOG = os.environ.get("LAB_REQUEST_LOG") == "1"
LOG_FIELD = os.environ.get("LAB_LOG_FIELD") == "1"
DELAY = _number("LAB_DELAY_MS", 400) / 1000
SLOW_ERRORS = os.environ.get("LAB_SLOW_ERRORS", "1") != "0"
SLOW_SHARE = min(1.0, _number("LAB_SLOW_SHARE", 1))
ERROR_FRACTION = min(1.0, _number("LAB_ERROR_FRACTION", 0))
SERVICE = _number("LAB_SERVICE_MS", 10) / 1000
WORK = _number("LAB_WORK_MS", 5) / 1000
READY_DELAY = _number("LAB_READY_DELAY_S", 0)
if PROFILE not in {"plain", "json", "kv"}:
    raise SystemExit("LAB_LOG_PROFILE must be plain, json or kv")


def choose(options, salt=""):
    """A stable choice per seed, so a trial's templates differ from another trial's."""
    index = int(hashlib.sha256(f"{SEED}:{salt}".encode()).hexdigest(), 16)
    return options[index % len(options)]


def _fields(fields):
    return " ".join(
        f"{key}={json.dumps(value) if isinstance(value, str) and ' ' in value else value}"
        for key, value in fields.items()
        if key != "logger"
    )


def log(level, message, **fields):
    """One log line in the selected profile. Plain keeps the original startup lines."""
    if PROFILE == "json":
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level.lower(),
            "logger": fields.pop("logger", "checkout"),
            "msg": message,
            **fields,
        }
        print(json.dumps(record, sort_keys=True), flush=True)
    elif PROFILE == "kv":
        extra = _fields(fields)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        print(f'time={stamp} level={level.lower()} msg="{message}" {extra}'.rstrip(), flush=True)
    else:
        extra = _fields(fields)
        prefix = "" if level == "PLAIN" else f"{level} "
        print(f"{prefix}{message}{' ' + extra if extra else ''}", flush=True)


def observe(status, seconds):
    global TOTAL_SECONDS
    with LOCK:
        COUNTS[str(status)] += 1
        for boundary in BUCKETS:
            if seconds <= boundary:
                COUNTS[boundary] += 1
        TOTAL_SECONDS += seconds


def metrics():
    with LOCK:
        count = sum(value for key, value in COUNTS.items() if isinstance(key, str))
        lines = ["# TYPE lab_http_requests_total counter"]
        for status in ("200", "503"):
            lines.append(f'lab_http_requests_total{{status="{status}"}} {COUNTS[status]}')
        lines.append("# TYPE lab_http_request_duration_seconds histogram")
        for boundary in BUCKETS:
            lines.append(
                f'lab_http_request_duration_seconds_bucket{{le="{boundary}"}} {COUNTS[boundary]}'
            )
        lines.extend(
            [
                f'lab_http_request_duration_seconds_bucket{{le="+Inf"}} {count}',
                f"lab_http_request_duration_seconds_sum {TOTAL_SECONDS}",
                f"lab_http_request_duration_seconds_count {count}",
            ]
        )
        return "\n".join(lines) + "\n"


def _note(query):
    """The request's ``note`` field, printable and bounded; logged only when enabled."""
    values = parse_qs(query).get("note") or []
    text = "".join(ch for ch in (values[0] if values else "") if ch.isprintable())
    return text[:200]


def _share(number, fraction):
    """Deterministic spacing: request n is selected when the running share falls behind."""
    return int((number + 1) * fraction) > int(number * fraction)


def _failed(number):
    if MODE == "slow":
        return SLOW_ERRORS and number % 3 == 0
    if MODE == "errors" and ERROR_FRACTION > 0:
        return _share(number, ERROR_FRACTION)
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/metrics":
            body, status = metrics().encode(), 200
            content_type = "text/plain; version=0.0.4"
        elif path == "/health":
            ready = time.monotonic() - STARTED >= READY_DELAY
            body, status, content_type = (
                (b"ready\n", 200, "text/plain") if ready else (b"warming\n", 503, "text/plain")
            )
        elif path == "/work":
            start = time.monotonic()
            acquired = SLOTS.acquire(blocking=False)
            status = 503
            number = None
            if acquired:
                try:
                    # Work and retained memory are bounded even during a sustained peak.
                    with LOCK:
                        ticket = TICKETS[0]
                        TICKETS[0] += 1
                    slow = MODE == "slow" and (SLOW_SHARE >= 1 or _share(ticket, SLOW_SHARE))
                    block = bytearray(1024 * 1024)
                    until = time.monotonic() + (0.08 if slow else WORK)
                    while time.monotonic() < until:
                        block[0] = (block[0] + 1) % 256
                    time.sleep(DELAY if slow else SERVICE)
                    with LOCK:
                        number = COUNTS["200"] + COUNTS["503"]
                    status = 503 if _failed(number) else 200
                finally:
                    SLOTS.release()
            elapsed = time.monotonic() - start
            observe(status, elapsed)
            if LOG_FIELD and parts.query:
                note = _note(parts.query)
                with LOCK:
                    due = time.monotonic() - LAST_NOTE[0] >= NOTE_EVERY_S
                    if due:
                        LAST_NOTE[0] = time.monotonic()
                if note and due:
                    log("INFO", "client note", logger="http", note=note)
            if REQUEST_LOG and (status != 200 or (number or 0) % 10 == 0):
                level = "ERROR" if status != 200 else "INFO"
                message = (
                    choose(("upstream busy", "worker pool saturated", "request rejected"), "busy")
                    if status != 200
                    else choose(("request completed", "handled request", "ok"), "done")
                )
                log(
                    level,
                    message,
                    logger="http",
                    path="/work",
                    status=status,
                    duration_ms=round(elapsed * 1000),
                )
            body, content_type = b"completed\n" if status == 200 else b"busy\n", "text/plain"
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


CRASHES = (
    ("KeyError", "'DATABASE_URL'"),
    ("ValueError", "invalid literal for int() with base 10: 'auto'"),
    ("ConnectionRefusedError", "[Errno 111] Connection refused"),
    ("RuntimeError", "schema migration 0042 did not complete"),
    ("PermissionError", "[Errno 13] Permission denied: '/var/run/checkout/cache'"),
)
SETTINGS = ("PAYMENT_GATEWAY_URL", "ORDER_QUEUE_NAME", "TAX_SERVICE_TOKEN", "INVENTORY_HOST")


def _traceback(where):
    kind, message = choose(CRASHES, "crash")
    log("ERROR", "Traceback (most recent call last):", logger="main")
    log(
        "ERROR",
        f'  File "/app/{where}.py", line {choose((41, 87, 112, 160), where)}, in start',
        logger="main",
    )
    log("ERROR", f"{kind}: {message}", logger="main")
    return f"{kind}: {message}"


def _allocate(total_mib, step_mib=8, pause=0.25):
    blocks = []
    remaining = total_mib
    while remaining > 0:
        size = min(step_mib, remaining)
        block = bytearray(int(size * 1024 * 1024))
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
        remaining -= size
        log("PLAIN" if PROFILE == "plain" else "INFO", "startup progress: preparing working data")
        time.sleep(pause)
    return blocks


def _background(target):
    threading.Thread(target=target, daemon=True).start()


def _warnings():
    every = _number("LAB_WARN_EVERY_S", 0)
    if not every:
        return
    messages = (
        ("cache refresh took longer than expected", {"duration_ms": 1240}),
        ("certificate for metrics endpoint expires soon", {"days": 29}),
        ("slow DNS lookup, retrying", {"host": "tax.internal", "ms": 180}),
        ("deprecated configuration key in use", {"key": "http.keepalive"}),
    )

    def run():
        index = 0
        while True:
            time.sleep(every)
            message, fields = messages[(index + len(SEED)) % len(messages)]
            log("WARN", message, logger="maintenance", **fields)
            index += 1

    _background(run)


def _once(kind):
    folder = Path(os.environ.get("LAB_STATE_DIR", "/tmp"))
    marker = folder / "started-once"
    if marker.exists():
        log("INFO", "previous run state found; resuming normal service", logger="main")
        return
    marker.write_text("1\n")
    delay = _number("LAB_ONCE_AFTER_S", 10)

    def fail():
        time.sleep(delay)
        if kind == "oom":
            log("WARN", "rebuilding in-memory catalog index", logger="catalog")
            _allocate(_number("LAB_ALLOC_MIB", 256), step_mib=16, pause=0.05)
        else:
            _traceback("worker")
            os._exit(1)

    _background(fail)


def _leak():
    rate = _number("LAB_LEAK_MIB_PER_S", 1)

    def run():
        while True:
            time.sleep(1)
            block = bytearray(int(rate * 1024 * 1024))
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            RETAINED.append(block)

    _background(run)


def _crash_after():
    delay = _number("LAB_CRASH_AFTER_S", 20)

    def run():
        time.sleep(delay)
        _traceback("worker")
        os._exit(1)

    _background(run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    if os.environ.get("LAB_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if MODE in {"crash", "oom"} or args.init:
        for _ in range(8):
            log(
                "PLAIN" if PROFILE == "plain" else "INFO",
                "startup progress: loading application configuration",
            )
            time.sleep(1)
        if MODE == "oom" and not args.init:
            _allocate(_number("LAB_ALLOC_MIB", 128))
            raise SystemExit("Memory limit did not terminate the bounded allocation.")
        if SEED:
            raise SystemExit(_traceback("main"))
        raise SystemExit("startup configuration rejected")
    if MODE == "setting":
        log("INFO", "loading application configuration", logger="config")
        name = choose(SETTINGS, "setting")
        log("ERROR", f"configuration error: required setting {name} is not set", logger="config")
        raise SystemExit(choose((1, 2, 78), "exit"))
    if MODE == "hold":
        RETAINED.extend(_allocate(_number("LAB_HOLD_MIB", 64), step_mib=8, pause=0.05))
    once = os.environ.get("LAB_ONCE")
    if once in {"crash", "oom"}:
        _once(once)
    if MODE == "leak":
        _leak()
    if MODE == "crash_after":
        _crash_after()
    _warnings()
    if SEED:
        log("INFO", "application listening", logger="main", port=8080)
    else:
        print("application listening", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
