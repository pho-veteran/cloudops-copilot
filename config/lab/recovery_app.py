"""Request-derived recovery sample: fixed serving capacity and a bounded resident working set."""

import os
import threading
import time
from http.server import ThreadingHTTPServer

from sample_app import Handler as SampleHandler, observe

SLOTS = threading.BoundedSemaphore(2)
WORKING_SET = None


class Handler(SampleHandler):
    def do_GET(self):
        if self.path != "/work":
            return super().do_GET()
        started = time.monotonic()
        acquired = SLOTS.acquire(timeout=0.2)
        status = 503
        if acquired:
            try:
                time.sleep(0.1)
                status = 200
            finally:
                SLOTS.release()
        observe(status, time.monotonic() - started)
        body = b"completed\n" if status == 200 else b"busy\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    request_queue_size = 32


def main():
    global WORKING_SET
    variant = os.environ.get("E07_SAMPLE_VARIANT", "healthy")
    if variant == "bad-image":
        raise SystemExit("startup configuration rejected by this image version")
    if variant != "healthy":
        raise SystemExit("unsupported image version")
    WORKING_SET = bytearray(160 * 1024 * 1024)
    # Touch each page so the bounded working set is resident before readiness.
    for offset in range(0, len(WORKING_SET), 4096):
        WORKING_SET[offset] = 1
    print("bounded working set ready; application listening", flush=True)
    Server(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
