"""Pinned integration seam: exact object selection and pre-rounding observed statistics."""

import json
import math
import runpy
import sys
import threading
from pathlib import Path

from krr_protocol import SOURCE_TREE_SHA256, digest, parse_json


def run(request_path, observations_path, arguments):
    body = parse_json(Path(request_path).read_bytes())
    target = body["identity"]
    sys.path.insert(0, "/app")
    import robusta_krr
    from kubernetes import client
    from robusta_krr.core import runner
    from robusta_krr.core.models.config import settings
    from robusta_krr.strategies.simple import SimpleStrategy

    root = Path("/app")
    files = [root / "krr.py", *(root / "robusta_krr").rglob("*.py")]
    import hashlib

    manifest = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }
    if robusta_krr.__version__ != "dev" or digest(manifest) != SOURCE_TREE_SHA256:
        raise ValueError("Pinned KRR source or embedded metadata differs.")
    original_list = client.AppsV1Api.list_namespaced_deployment

    def exact_deployment(self, namespace, **kwargs):
        if namespace != target["namespace"]:
            raise ValueError("KRR attempted a foreign namespace.")
        kwargs["field_selector"] = "metadata.name=" + target["name"]
        kwargs["_request_timeout"] = (4, 8)
        return original_list(self, namespace, **kwargs)

    client.AppsV1Api.list_namespaced_deployment = exact_deployment

    async def no_remote_version_check():
        return None

    runner.load_latest_version = no_remote_version_check
    original_run = SimpleStrategy.run
    observations, parameters, lock = [], {}, threading.Lock()

    def capture(self, history, obj):
        if any(
            (
                settings.publish_scan_url,
                settings.named_sinks,
                settings.slack_output,
                settings.azureblob_output,
                settings.teams_webhook,
            )
        ):
            raise ValueError("External result sinks are disabled.")
        parameters.update(
            cpu_min=settings.cpu_min_value,
            mem_min=settings.memory_min_value,
            max_workers=settings.max_workers,
        )
        result = original_run(self, history, obj)
        value = {
            "namespace": obj.namespace,
            "name": obj.name,
            "kind": obj.kind,
            "container": obj.container,
        }
        for resource, loader in (("cpu", "PercentileCPULoader"), ("memory", "MaxMemoryLoader")):
            metrics = history.get(loader, {})
            if len(metrics) > 100:
                raise ValueError("Metric Pod bound exceeded.")
            if sum(len(rows) for rows in metrics.values()) > 200_000:
                raise ValueError("Metric sample bound exceeded.")
            numbers = []
            for rows in metrics.values():
                if len(rows.shape) != 2 or rows.shape[1] != 2 or len(rows) > 200_000:
                    raise ValueError("Metric shape differs.")
                numbers.extend(float(number) for number in rows[:, 1])
            observed = (
                max(numbers)
                if numbers and all(math.isfinite(x) and x >= 0 for x in numbers)
                else None
            )
            value[resource] = {"observed": observed, "pods": sorted(metrics)}
        with lock:
            if len(observations) >= 32:
                raise ValueError("Container bound exceeded.")
            observations.append(value)
        return result

    SimpleStrategy.run = capture
    sys.argv = ["/app/krr.py", *arguments]
    try:
        runpy.run_path("/app/krr.py", run_name="__main__")
    finally:
        Path(observations_path).write_text(
            json.dumps(
                {
                    "request_sha256": digest(body),
                    "parameters": parameters,
                    "observations": observations,
                },
                allow_nan=False,
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3:])
