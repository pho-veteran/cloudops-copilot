"""Finite offered load for the owned recovery checkout Service; no arbitrary destination."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from load import run


class OfferedLoad:
    def __init__(self):
        self.attempts = 0
        self.lock = threading.Lock()

    def attempted(self):
        with self.lock:
            self.attempts += 1

    def metrics(self):
        with self.lock:
            return (
                "# TYPE lab_offered_requests_total counter\n"
                f"lab_offered_requests_total {self.attempts}\n"
            ).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--rate", type=int, default=30)
    options = parser.parse_args()
    if not 1 <= options.seconds <= 900:
        raise ValueError("Recovery trials must last 1..900 seconds.")
    offered = OfferedLoad()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = offered.metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run(
            "checkout", options.seconds, options.rate, concurrency=16, on_attempt=offered.attempted
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(json.dumps({**result, "concurrency": 16}))


if __name__ == "__main__":
    main()
