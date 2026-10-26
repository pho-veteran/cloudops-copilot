"""One configured, read-only KRR invocation; no arbitrary command, endpoint or sink."""

import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kubernetes import client, config

LOCK = threading.Semaphore(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        key = Path("/run/cloudops-krr/key").read_text().strip()
        if self.path != "/report" or not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), ("Bearer " + key).encode()
        ):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16384:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if (
                set(body) != {"namespace", "name", "uid", "requested_hours"}
                or type(body["requested_hours"]) is not int
                or not 6 <= body["requested_hours"] <= 24
            ):
                raise ValueError
            allowlist = json.loads(Path("/run/cloudops-reader/workloads.json").read_text())
            if not any(
                all(item[k] == body[k] for k in ("namespace", "name", "uid")) for item in allowlist
            ):
                raise ValueError
        except (ValueError, KeyError):
            self.send_error(422)
            return
        if not LOCK.acquire(blocking=False):
            self.send_error(429)
            return
        try:
            try:
                configuration = client.Configuration()
                config.load_kube_config(
                    config_file="/run/cloudops-reader/kubeconfig.json",
                    client_configuration=configuration,
                )
                with client.ApiClient(configuration) as connection:
                    deployment = client.AppsV1Api(connection).read_namespaced_deployment(
                        body["name"], body["namespace"], _request_timeout=8
                    )
                    if (
                        deployment.metadata.uid != body["uid"]
                        or (deployment.metadata.labels or {}).get("app") != body["name"]
                    ):
                        self.send_error(
                            409, "Configured live workload identity or selector changed"
                        )
                        return
            except Exception:
                self.send_error(503, "Configured live workload could not be verified")
                return
            with tempfile.TemporaryDirectory(prefix="cloudops-krr-") as folder:
                output = Path(folder) / "report.json"
                command = [
                    sys.executable,
                    "/app/krr.py",
                    "simple",
                    "--kubeconfig",
                    "/run/cloudops-reader/kubeconfig.json",
                    "--namespace",
                    body["namespace"],
                    "--resource",
                    "Deployment",
                    "--selector",
                    "app=" + body["name"],
                    "--prometheus-url",
                    "http://prometheus:9090",
                    "--history_duration",
                    str(body["requested_hours"]),
                    "--formatter",
                    "json",
                    "--fileoutput",
                    str(output),
                    "--logtostderr",
                ]
                code, digest = None, None
                try:
                    finished = subprocess.run(
                        command, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    code = finished.returncode
                    if output.is_file() and output.stat().st_size <= 2_000_000:
                        data = output.read_bytes()
                        json.loads(data)
                        digest = hashlib.sha256(data).hexdigest()
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                # Tool output remains unqualified until representative history is proven.
                result = {
                    "state": "insufficient_data" if code == 0 and digest else "unavailable",
                    "reason": "Pinned KRR ran against the scoped cluster and configured Prometheus. Representative idle/steady/peak coverage is not qualified; recommendations remain unavailable.",
                    "result": {
                        "source": "live_krr",
                        "tool_version": "1.31.1",
                        "requested_hours": body["requested_hours"],
                        "workload_uid": body["uid"],
                        "tool_exit_code": code,
                        "output_sha256": digest,
                        "observed_window": None,
                        "coverage": None,
                        "recommendations": None,
                        "limitations": "No qualified representative history; no apply operation or savings estimate.",
                    },
                }
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        finally:
            LOCK.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
