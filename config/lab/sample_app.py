"""Small measured HTTP workload; controls are deployment-owned and never HTTP inputs."""

import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time

BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
LOCK = threading.Lock()
COUNTS = Counter()
TOTAL_SECONDS = 0.0
SLOTS = threading.BoundedSemaphore(2)
MODE = os.environ.get("LAB_MODE", "healthy")


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/metrics":
            body, status = metrics().encode(), 200
            content_type = "text/plain; version=0.0.4"
        elif self.path == "/health":
            body, status, content_type = b"ready\n", 200, "text/plain"
        elif self.path == "/work":
            start = time.monotonic()
            acquired = SLOTS.acquire(blocking=False)
            status = 503
            if acquired:
                try:
                    # Work and retained memory are bounded even during a sustained peak.
                    block = bytearray(1024 * 1024)
                    until = time.monotonic() + (0.08 if MODE == "slow" else 0.005)
                    while time.monotonic() < until:
                        block[0] = (block[0] + 1) % 256
                    time.sleep(0.4 if MODE == "slow" else 0.01)
                    with LOCK:
                        number = COUNTS["200"] + COUNTS["503"]
                    status = 503 if MODE == "slow" and number % 3 == 0 else 200
                finally:
                    SLOTS.release()
            observe(status, time.monotonic() - start)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    if MODE in {"crash", "oom"} or args.init:
        for _ in range(8):
            print("startup progress: loading application configuration", flush=True)
            time.sleep(1)
        if MODE == "oom" and not args.init:
            blocks = []
            for _ in range(16):
                blocks.append(bytearray(8 * 1024 * 1024))
                print("startup progress: preparing working data", flush=True)
                time.sleep(0.25)
            raise SystemExit("Memory limit did not terminate the bounded allocation.")
        raise SystemExit("startup configuration rejected")
    print("application listening", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
