"""Labeled lab-only metrics fixture; it is not a product application exporter."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKLOAD_FILE = Path(os.environ.get("WORKLOAD_FILE", "/run/cloudops-prometheus/workloads.json"))
MODE = os.environ.get("METRICS_FIXTURE_MODE", "finite")
LOCK = threading.Lock()
SCRAPES = 0


def workloads():
    value = json.loads(WORKLOAD_FILE.read_text(encoding="utf-8"))
    rows = value if isinstance(value, list) else [value]
    result = []
    for row in rows:
        namespace = str(row["namespace"])
        name = str(row["name"])
        uid = str(row["uid"])
        if not namespace or not name or not uid:
            raise ValueError
        result.append((namespace, name, uid))
    if not result:
        raise ValueError
    return result


def labels(namespace, name, uid):
    return f'namespace="{namespace}",deployment_uid="{uid}",pod="{name}-fixture",container="{name}"'


def metrics():
    with LOCK:
        global SCRAPES
        SCRAPES += 1
        scrape = SCRAPES
    if MODE == "empty":
        return []
    if MODE == "gapped" and scrape % 4 in (1, 2):
        # The series appear intermittently to exercise retained gaps.
        return []
    lines = []
    infinity = MODE == "nonfinite"
    for namespace, name, uid in workloads():
        common = labels(namespace, name, uid)
        lines.extend(
            [
                f"cloudops_requests_per_second{{{common}}} {'NaN' if infinity else '12'}",
                f"cloudops_error_ratio{{{common}}} {'NaN' if infinity else '0.02'}",
                f"cloudops_p95_latency_seconds{{{common}}} {'NaN' if infinity else '0.25'}",
                f"cloudops_memory_working_set_bytes{{{common}}} {'NaN' if infinity else '33554432'}",
                f"cloudops_memory_limit_bytes{{{common}}} {'NaN' if infinity else '67108864'}",
                f"cloudops_restarts_total{{{common}}} {'NaN' if infinity else '0'}",
            ]
        )
    return lines


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            body = ("\n".join(metrics()) + "\n").encode("utf-8")
        except (KeyError, OSError, TypeError, ValueError):
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
