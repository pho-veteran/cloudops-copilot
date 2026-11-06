"""Measure a finite pure comparison ceiling; no provider, database or SLO claim."""

import argparse
import json
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cloudops.features.investigations.comparison import differences
from cloudops.features.investigations.comparison_service import page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    metadata = SimpleNamespace(
        id=uuid4(),
        run_id=uuid4(),
        payload_kind="series",
        payload_version=1,
        coverage=SimpleNamespace(status="available"),
        query_metadata={
            "query_id": "request_rate",
            "query_version": "fixture_v1",
            "unit": "requests/second",
            "source_identity": {"endpoint_id": "fixture"},
            "step": 30,
        },
        requested_window="mechanical-benchmark-window",
    )
    payload = {
        "series": [
            {
                "labels": {"pod_uid": f"fixture-{series}"},
                "unit": "requests/second",
                "samples": [[sample, sample % 5] for sample in range(800)],
            }
            for series in range(25)
        ]
    }
    tracemalloc.start()
    start = time.perf_counter()
    rows, cursor = page(
        differences(metadata, metadata, payload, payload), "mechanical-benchmark", None, 100
    )
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = {
        "proof_class": "Mechanical persisted-shape ceiling benchmark; not a live collector, valid exporter cadence or service SLO.",
        "series_per_side": 25,
        "samples_per_side": 20000,
        "payload_bytes_per_side": len(json.dumps(payload, separators=(",", ":")).encode()),
        "page_rows": len(rows),
        "has_more": cursor is not None,
        "item_bytes": sum(len(row.model_dump_json().encode()) for row in rows),
        "elapsed_seconds_with_tracemalloc": round(elapsed, 4),
        "traced_peak_bytes": peak,
    }
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")


if __name__ == "__main__":
    main()
