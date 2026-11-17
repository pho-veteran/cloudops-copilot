"""One authenticated, finite, read-only invocation of the pinned KRR profile."""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from cluster_identity import cluster_fingerprint
from krr_protocol import (
    ContractError,
    VERSION,
    bound_snapshot,
    digest,
    normalize,
    parse_json,
    snapshot,
    validate_request,
)

LOCK = threading.Semaphore(1)
READER = Path("/run/cloudops-reader")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def read_json(path, maximum=100_000):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ContractError("runner_configuration_invalid")
    return parse_json(raw)


def connection(identity):
    reader = read_json(READER / "client.json")
    if cluster_fingerprint(reader) != identity["cluster_fingerprint"]:
        raise ContractError("runner_cluster_mismatch")
    kube = read_json(READER / "kubeconfig.json")
    if len(kube["clusters"]) != 1 or len(kube["users"]) != 1 or len(kube["contexts"]) != 1:
        raise ContractError("runner_configuration_invalid")
    cluster, user, context = kube["clusters"][0], kube["users"][0], kube["contexts"][0]
    if (
        set(cluster["cluster"]) != {"server", "certificate-authority-data"}
        or set(user["user"]) != {"token"}
        or set(context["context"]) - {"cluster", "user", "namespace"}
        or kube["current-context"] != context["name"]
        or (context["context"]["cluster"], context["context"]["user"])
        != (cluster["name"], user["name"])
        or user["user"]["token"] != reader["token"]
    ):
        raise ContractError("runner_configuration_invalid")
    if (
        cluster_fingerprint(
            {
                "server": cluster["cluster"]["server"],
                "ca_data": cluster["cluster"]["certificate-authority-data"],
            }
        )
        != identity["cluster_fingerprint"]
    ):
        raise ContractError("runner_cluster_mismatch")
    allowed = read_json(READER / "workloads.json")
    if (
        not isinstance(allowed, list)
        or len(allowed) > 100
        or not any(
            all(item[k] == identity[k] for k in ("namespace", "name", "uid")) for item in allowed
        )
    ):
        raise ContractError("runner_target_unproven")
    if not isinstance(reader["token"], str) or not 1 <= len(reader["token"]) <= 4096:
        raise ContractError("runner_configuration_invalid")
    context = ssl.create_default_context(
        cadata=base64.b64decode(reader["ca_data"], validate=True).decode()
    )
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
    path = f"/apis/apps/v1/namespaces/{quote(identity['namespace'], safe='')}/deployments/{quote(identity['name'], safe='')}"
    request = Request(
        reader["server"].rstrip("/") + path,
        headers={"Authorization": "Bearer " + reader["token"], "Accept-Encoding": "identity"},
    )
    with opener.open(request, timeout=8) as response:
        raw = response.read(2_000_001)
        if (
            len(raw) > 2_000_000
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise ContractError("runner_target_unproven")
    current = snapshot(parse_json(raw))
    bound_snapshot(current, identity)
    return current


def command(folder, body):
    return [
        sys.executable,
        "/cloudops-krr-entry.py",
        str(folder / "request.json"),
        str(folder / "observations.json"),
        "simple",
        "--kubeconfig",
        str(READER / "kubeconfig.json"),
        "--namespace",
        body["identity"]["namespace"],
        "--resource",
        "Deployment",
        "--prometheus-url",
        "http://prometheus:9090",
        "--history_duration",
        "6",
        "--timeframe_duration",
        "1.25",
        "--cpu_percentile",
        "95",
        "--memory_buffer_percentage",
        "15",
        "--points_required",
        "100",
        "--cpu-min",
        "10",
        "--mem-min",
        "100",
        "--max-workers",
        "2",
        "--formatter",
        "json",
        "--fileoutput",
        str(folder / "report.json"),
        "--logtostderr",
    ]


def invoke(body):
    result = {
        "protocol_version": 1,
        "request_sha256": digest(body),
        "state": "unavailable",
        "reason": "runner_failed",
        "tool_version": VERSION,
        "image_digest": body["profile"]["image_digest"],
        "invocation_started_at": None,
        "invocation_finished_at": None,
        "tool_exit_code": None,
        "timed_out": False,
        "output_sha256": None,
        "scope_before": None,
        "scope_after": None,
        "containers": [],
    }
    try:
        before = connection(body["identity"])
        result["scope_before"] = before
        with tempfile.TemporaryDirectory(prefix="cloudops-krr-") as temporary:
            folder = Path(temporary)
            (folder / "request.json").write_text(json.dumps(body, allow_nan=False))
            env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
            env.update(
                MPLCONFIGDIR=str(folder / "mpl"), OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"
            )
            result["invocation_started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                finished = subprocess.run(
                    command(folder, body),
                    env=env,
                    timeout=120,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                result["tool_exit_code"] = finished.returncode
            except subprocess.TimeoutExpired:
                result["reason"] = "runner_timeout"
                result["timed_out"] = True
            finally:
                result["invocation_finished_at"] = datetime.now(timezone.utc).isoformat()
            after = connection(body["identity"])
            result["scope_after"] = after
            if before != after:
                raise ContractError("runner_target_changed")
            output = folder / "report.json"
            if not output.is_file():
                return result
            with output.open("rb") as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ContractError("runner_payload_limit")
            result["output_sha256"] = hashlib.sha256(raw).hexdigest()
            if result["tool_exit_code"] != 0:
                return result
            normalized = normalize(
                parse_json(raw), read_json(folder / "observations.json", 262_144), body
            )
            result.update(state="completed", reason="tool_completed", containers=normalized)
    except ContractError as error:
        result["reason"] = error.reason
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["reason"] = "runner_source_unavailable"
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, value, status=200):
        payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 262_144:
            payload = json.dumps(
                {"protocol_version": 1, "state": "unavailable", "reason": "runner_payload_limit"}
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.connection.settimeout(5)
        try:
            key = Path("/run/cloudops-krr/key").read_text().strip()
            if (
                self.path != "/report"
                or not key
                or not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), ("Bearer " + key).encode()
                )
            ):
                self.respond({"reason": "runner_authentication_failed"}, 403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16_384:
                raise ContractError("runner_request_invalid")
            body = validate_request(
                parse_json(self.rfile.read(length)), os.environ.get("KRR_RUNNER_IMAGE_DIGEST", "")
            )
        except (ContractError, ValueError, OSError):
            self.respond({"reason": "runner_request_invalid"}, 422)
            return
        if not LOCK.acquire(blocking=False):
            self.respond({"reason": "runner_busy"}, 429)
            return
        try:
            self.respond(invoke(body))
        finally:
            LOCK.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
