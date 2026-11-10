"""Write the scoped Prometheus read configuration from the lab TLS authority."""

import base64
import json
import subprocess

from local_lab import COMPOSE, ROOT

FOLDER = ROOT / "tmp/e01-integration/prometheus"
QUERIES = {
    "request_rate": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_requests_per_second{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "requests/s",
    },
    "error_ratio": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_error_ratio{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "ratio",
    },
    "p95_latency": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_p95_latency_seconds{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "seconds",
    },
    "memory_working_set": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_memory_working_set_bytes{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "bytes",
    },
    "memory_limit": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_memory_limit_bytes{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "bytes",
    },
    "restarts": {
        "query": (
            "sum by (namespace, deployment_uid) (cloudops_restarts_total{"
            'namespace="{{namespace}}",deployment_uid="{{workload_uid}}"})'
        ),
        "unit": "count",
    },
}


def main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    workload = FOLDER / "workload.json"
    if not workload.is_file():
        raise SystemExit("Run scripts/bootstrap-integration.py first; workload.json is missing.")
    copied = subprocess.run(
        [
            *COMPOSE,
            "cp",
            "https:/data/caddy/pki/authorities/local/root.crt",
            str(FOLDER / "ca.crt"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if copied.returncode:
        raise SystemExit(
            "Start the HTTPS proxy first, then rerun this script. The Caddy CA was not copied."
        )
    config = {
        "schema": "PrometheusReadConfig/v1",
        "endpoint_id": "lab-prometheus",
        "producer_version": "prometheus/v3.5.0",
        "server": "https://https:8443/prometheus",
        "ca_data": base64.b64encode((FOLDER / "ca.crt").read_bytes()).decode(),
        "step_seconds": 30,
        "binding": {
            "deployment_uid_label": "deployment_uid",
            "namespace_label": "namespace",
        },
        "queries": QUERIES,
    }
    (FOLDER / "read.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("Prometheus read configuration written; no CA or token value was printed.")


if __name__ == "__main__":
    main()
