"""Finite load against one of two owned Services; no arbitrary destination or request data."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def run(target, seconds, rate, *, concurrency=4, on_attempt=None):
    if (
        target not in {"checkout", "history", "both"}
        or not 1 <= seconds <= 21600
        or not 1 <= rate <= 30
        or type(concurrency) is not int
        or not 1 <= concurrency <= 16
    ):
        raise ValueError(
            "Use an owned target, 1..21600 seconds, 1..30 requests/second and 1..16 clients."
        )
    targets = ("checkout", "history") if target == "both" else (target,)
    counts, lock = Counter(), threading.Lock()
    slots = threading.BoundedSemaphore(concurrency)
    opener = build_opener(ProxyHandler({}))

    def request(destination):
        url = f"http://k3s:{30080 if destination == 'checkout' else 30081}/work"
        try:
            if on_attempt is not None:
                on_attempt()
            try:
                with opener.open(url, timeout=2) as response:
                    response.read(1024)
                    result = str(response.status)
            except HTTPError as error:
                result = str(error.code)
                error.close()
            except (URLError, OSError, TimeoutError):
                result = "transport_error"
            with lock:
                counts[result] += 1
        finally:
            slots.release()

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for index in range(seconds * rate):
            delay = start + index / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if slots.acquire(blocking=False):
                pool.submit(request, targets[index % len(targets)])
            else:
                with lock:
                    counts["client_capacity_skipped"] += 1
    return {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "seconds": seconds,
        "rate": rate,
        "responses": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["checkout", "history", "both"], default="checkout")
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--rate", type=int, default=5)
    options = parser.parse_args()
    print(json.dumps(run(options.target, options.seconds, options.rate)))
