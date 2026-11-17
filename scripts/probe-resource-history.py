"""Finite owned-lab history recipe; short runs never qualify six-hour advice."""

import argparse
import fcntl
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from local_lab import OWNED_PROJECT, private_write

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def local_origin(value):
    url = urlsplit(value)
    require(
        url.scheme in {"http", "https"}
        and url.path in {"", "/"}
        and not (url.query or url.fragment or url.username or url.password),
        "Use one local API/web origin without credentials or a path",
    )
    require(
        url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback,
        "Only loopback acceptance endpoints are allowed",
    )
    require(url.port is None or 1 <= url.port <= 65535, "Invalid local port")
    return value.rstrip("/")


def config(path):
    require(
        path.is_file() and not path.is_symlink() and not path.stat().st_mode & 0o077,
        "Use a private regular lab configuration",
    )
    value = json.loads(path.read_text())
    require(
        set(value)
        == {
            "lab_root",
            "owner",
            "worker_container",
            "runtime_containers",
            "api_url",
            "web_origin",
            "operator",
            "workload_id",
            "ca_file",
        },
        "Unexpected history-lab configuration fields",
    )
    UUID(value["owner"])
    UUID(value["workload_id"])
    value["api_url"] = local_origin(value["api_url"])
    value["web_origin"] = local_origin(value["web_origin"])
    ids = value["runtime_containers"]
    require(
        isinstance(ids, list)
        and 1 <= len(ids) <= 12
        and len(set(ids)) == len(ids)
        and value["worker_container"] in ids
        and all(
            isinstance(cid, str) and len(cid) == 64 and all(c in "0123456789abcdef" for c in cid)
            for cid in ids
        ),
        "Pin exact owned runtime container IDs",
    )
    return value


def owned_lab(value):
    root = Path(value["lab_root"])
    require(root.is_absolute() and root.resolve() == root, "Use the exact owned checkout")
    directory = root / "tmp/local-testing-lab"
    marker = json.loads((directory / "owner.json").read_text())
    require(
        marker.get("root") == str(root)
        and marker.get("owner") == value["owner"]
        and marker.get("project") == OWNED_PROJECT,
        "Owned lab marker differs",
    )
    for name in ("Dockerfile.sample", "sample_app.py"):
        require(
            (root / "config/lab" / name).read_bytes() == (ROOT / "config/lab" / name).read_bytes(),
            "Retained sample/load implementation differs from this recipe",
        )
    spec = importlib.util.spec_from_file_location(
        "history_owned_lab", ROOT / "scripts/manage-local-lab.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT, module.DIRECTORY = root, directory
    lab = module.Lab(directory)
    lab.ownership()
    return lab


class Recipe:
    def __init__(self, lab, value, output, *, short=False, clock=time.monotonic, sleep=time.sleep):
        self.lab, self.config, self.output = lab, value, output
        self.clock, self.sleep = clock, sleep
        self.warmup, self.phase_seconds, self.hold = (3, 15, 90) if short else (180, 7200, 300)
        self.cap = self.warmup + 3 * self.phase_seconds + self.hold
        self.cleanup_reserve = 20
        self.short, self.load_id, self.initial = short, None, None
        self.record = {
            "cohort_id": output.name,
            "short_harness_only": short,
            "qualified": False,
            "cap_seconds": self.cap,
            "phases": [],
            "checkpoints": [],
            "source_sha256": {
                path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                for path in (
                    "scripts/probe-resource-history.py",
                    "scripts/resource_history_source.py",
                    "scripts/manage-local-lab.py",
                    "scripts/local_lab.py",
                    "config/lab/load.py",
                )
            },
        }

    def save(self):
        private_write(self.output / "receipt.json", json.dumps(self.record, indent=2) + "\n")

    def inspect(self, cid):
        row = json.loads(self.lab.command(["docker", "inspect", cid]))[0]
        labels = row["Config"].get("Labels", {})
        require(
            row["Id"] == cid
            and (
                labels.get("io.cloudops.lab.owner") == self.config["owner"]
                or labels.get("io.cloudops.e08.owner") == "report-delivery"
            ),
            "Runtime container ownership differs",
        )
        return row

    def footprint(self):
        rows = self.lab.ownership()["container"]
        rows = {r["Id"]: r for r in rows}
        rows.update({cid: self.inspect(cid) for cid in self.config["runtime_containers"]})
        prometheus = [
            row
            for row in rows.values()
            if row["State"]["Running"]
            and row["Config"].get("Labels", {}).get("com.docker.compose.service") == "prometheus"
        ]
        require(
            len(prometheus) == 1
            and {"--storage.tsdb.retention.time=48h", "--storage.tsdb.retention.size=2GB"}
            <= set(prometheus[0]["Config"]["Cmd"]),
            "Owned Prometheus needs bounded 48h/2GB retention",
        )
        worker_image = rows[self.config["worker_container"]]["Image"]
        for role in ("api", "worker", "observer"):
            matches = [
                row
                for row in rows.values()
                if row["State"]["Running"]
                and any(
                    f"cloudops.entrypoints.{role}" in arg
                    for arg in (row["Config"].get("Cmd") or [])
                )
            ]
            require(
                len(matches) == 1 and matches[0]["Image"] == worker_image,
                "API, worker and observer must use one upgraded backend image",
            )
        feature_ids = set(
            self.lab.command(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--no-trunc",
                    "--filter",
                    "label=io.cloudops.e08.owner=report-delivery",
                ]
            ).split()
        )
        require(
            feature_ids <= rows.keys(),
            "A running feature container is missing from the budget inventory",
        )
        running = [r for r in rows.values() if r["State"]["Running"]]
        require(
            all(
                r["State"]["Running"] and not r["State"]["Paused"]
                for cid, r in rows.items()
                if cid in self.config["runtime_containers"]
            ),
            "Required runtime stopped or paused",
        )
        require(
            all(
                0 < r["HostConfig"]["Memory"] == r["HostConfig"]["MemorySwap"]
                and r["HostConfig"]["NanoCpus"] > 0
                and r["HostConfig"]["LogConfig"]["Config"] == {"max-size": "5m", "max-file": "2"}
                for r in running
            ),
            "Every running owned service needs cgroup/log bounds",
        )
        require(
            sum(r["HostConfig"]["Memory"] for r in running) <= 8.5 * GIB
            and sum(r["HostConfig"]["NanoCpus"] for r in running) <= 8_000_000_000,
            "Configured owned service envelope exceeds 8.5 GiB/eight CPUs",
        )
        import shutil

        require(shutil.disk_usage(self.lab.directory).free >= 5 * GIB, "Disk headroom exhausted")
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        require(available >= 2 * GIB, "Host memory headroom exhausted")
        volumes, images = {}, set()
        disk_bytes = 0
        for row in running:
            images.add(row["Image"])
            for mount in row.get("Mounts", []):
                if mount["Type"] in {"volume", "bind"} and mount.get("RW"):
                    volumes.setdefault(
                        mount.get("Name", mount["Source"]), (row["Id"], mount["Destination"])
                    )
        measured = json.loads(
            self.lab.command(["docker", "inspect", "--size", *[r["Id"] for r in running]])
        )
        require(
            all(isinstance(r.get("SizeRw"), int) and r["SizeRw"] >= 0 for r in measured),
            "Writable layer footprint is unavailable",
        )
        disk_bytes += sum(r["SizeRw"] for r in measured)
        for cid, path in volumes.values():
            size = self.lab.command(["docker", "exec", cid, "du", "-sk", path], timeout=15)
            disk_bytes += int(size.split()[0]) * 1024
        for row in json.loads(self.lab.command(["docker", "image", "inspect", *sorted(images)])):
            disk_bytes += row["Size"]
        disk_bytes += sum(
            file.stat().st_size
            for file in self.lab.directory.rglob("*")
            if file.is_file() and not file.is_symlink()
        )
        disk_bytes += len(running) * 10 * 1024**2
        require(
            disk_bytes <= 20 * GIB, "Owned volume/image/evidence/log disk envelope exceeds 20 GiB"
        )
        stats = json.loads(
            "["
            + ",".join(
                self.lab.command(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        *[r["Id"] for r in running],
                    ],
                    timeout=15,
                ).splitlines()
            )
            + "]"
        )
        return {
            "disk_bytes_upper_bound": disk_bytes,
            "containers": sorted(
                [
                    {
                        "id": r["Id"],
                        "image": r["Image"],
                        "restarts": r["RestartCount"],
                        "started_at": r["State"]["StartedAt"],
                    }
                    for r in running
                    if r["Id"] != self.load_id
                ],
                key=lambda r: r["id"],
            ),
            "stats": stats,
        }

    def checkpoint(self, *, full=False):
        self.inspect(self.config["worker_container"])
        require(
            all(
                hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected
                for path, expected in self.record["source_sha256"].items()
            ),
            "Recipe/load source changed; start a separately identified cohort",
        )
        self.lab.credentials_valid(read_only=True)
        for kind in ("reader", "monitoring"):
            expiry = json.loads((self.lab.directory / kind / "expiry.json").read_text())[
                "expires_at"
            ]
            require(
                datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                > time.time() + (self.cap if self.initial is None else 30),
                f"{kind} credential does not cover the bounded recipe",
            )
        command = [
            "docker",
            "exec",
            "-i",
            self.config["worker_container"],
            "python",
            "-",
            self.config["workload_id"],
            *(["--full"] if full else []),
            *(
                ["--deadline", str(self.record["source_deadline_unix"])]
                if "source_deadline_unix" in self.record
                else []
            ),
        ]
        process = subprocess.run(
            command,
            input=(ROOT / "scripts/resource_history_source.py").read_text(),
            env=self.lab.env,
            cwd=self.lab.directory,
            text=True,
            capture_output=True,
            timeout=65,
        )
        raw = process.stdout
        require(len(raw) <= 2_000_000, "Checkpoint output exceeded its bound")
        source = (
            json.loads(raw) if raw else {"error": "Checkpoint process failed or exceeded deadline"}
        )
        require(
            process.returncode == 0 and "error" not in source,
            "Source/retention/producer readiness failed: "
            + str(source.get("error", "Unknown failure"))[:512],
        )
        require(
            source["identity"]["uid"] == self.lab.marker["deployments"]["checkout"]
            and source["identity"]["namespace"] == "demo"
            and source["identity"]["name"] == "checkout",
            "Source target differs from owned checkout",
        )
        for name in ("cpu", "memory"):
            for row in source["measurements"][name]:
                points = row["samples"]
                require(
                    points
                    and all(
                        math.isfinite(float(t)) and math.isfinite(float(v)) and float(v) >= 0
                        for t, v in points
                    )
                    and time.time() - float(points[-1][0]) <= 120,
                    "Fresh finite raw metric samples required",
                )
        footprint = self.footprint()
        stable = {
            "identity": source["identity"],
            "profile": source["profile"],
            "metrics_attestation": source["metrics_attestation"],
            "pods": sorted(p["uid"] for p in source["pods"]),
            "containers": footprint["containers"],
        }
        if self.initial is None:
            self.initial = stable
        require(
            stable == self.initial,
            "UID/template/source/producer or runtime restart changed; start a new cohort",
        )
        self.record["checkpoints"].append({"at": utc(), "source": source, "footprint": footprint})
        self.save()
        return source

    def stop_load(self):
        ids = self.lab.command(
            [
                "docker",
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=io.cloudops.e08.cohort=" + self.output.name,
            ],
            timeout=3,
        ).split()
        require(len(ids) <= 1, "Unexpected load concurrency; cleanup refused")
        if ids:
            row = json.loads(self.lab.command(["docker", "inspect", ids[0]], timeout=3))[0]
            require(
                row["Config"]["Labels"].get("io.cloudops.e08.cohort") == self.output.name,
                "Load container ownership changed; refusing cleanup",
            )
            self.lab.command(["docker", "rm", "-f", ids[0]], timeout=5)
        self.load_id = None

    def start_load(self, seconds, rate):
        self.stop_load()
        self.load_id = self.lab.dc(
            "run",
            "-d",
            "--no-deps",
            "--volume",
            str(ROOT / "config/lab/load.py") + ":/app/load.py:ro",
            "--name",
            OWNED_PROJECT + "-history-" + self.output.name,
            "--label",
            "io.cloudops.e08.cohort=" + self.output.name,
            "load",
            "--target",
            "checkout",
            "--seconds",
            str(seconds),
            "--rate",
            str(rate),
            "--deadline",
            str(self.record["load_deadline_unix"]),
        ).strip()
        require(len(self.load_id) == 64, "Load did not return one container identity")

    def phase(self, name, seconds, rate):
        row = {"name": name, "started_at": utc(), "nominal_seconds": seconds, "requested_rps": rate}
        self.record["phases"].append(row)
        self.save()
        if rate:
            self.start_load(seconds, rate)
        end, next_check = self.clock() + seconds, self.clock() + min(180, seconds)
        while self.clock() < end:
            self.sleep(min(5, end - self.clock()))
            if self.clock() >= next_check and self.clock() < end:
                self.checkpoint()
                next_check = self.clock() + 180
        if rate:
            status = json.loads(self.lab.command(["docker", "wait", self.load_id], timeout=10))
            require(status == 0, "Finite HTTP load failed")
            logs = self.lab.command(["docker", "logs", self.load_id], timeout=5)
            require(len(logs) <= 65_536, "Load receipt exceeded its bound")
            row["measured_load"] = json.loads(logs)
            require(
                row["measured_load"]["completed_nominal_load"],
                "Load reached the absolute cohort deadline",
            )
            self.stop_load()
        row["finished_at"] = utc()
        self.checkpoint()

    def phase_intersections(self, window):
        if not window:
            return []
        start, end = (
            datetime.fromisoformat(window[k].replace("Z", "+00:00")) for k in ("start", "end")
        )
        return [
            {
                "phase": phase["name"],
                "seconds": max(
                    0,
                    (
                        min(end, datetime.fromisoformat(phase["finished_at"]))
                        - max(start, datetime.fromisoformat(phase["started_at"]))
                    ).total_seconds(),
                ),
            }
            for phase in self.record["phases"]
            if "finished_at" in phase
        ]

    def run(self, client, headers):
        self.lab.containment(read_only=True)
        self.checkpoint(full=True)
        self.record["started_at"] = utc()
        self.record["load_deadline_unix"] = time.time() + self.cap - self.cleanup_reserve - 5
        self.record["source_deadline_unix"] = time.time() + self.cap - 5
        self.record["cleanup_reserve_seconds"] = self.cleanup_reserve
        start = self.clock()
        deadline = start + self.cap - self.cleanup_reserve

        def expired(*_):
            raise TimeoutError("Finite recipe cap reached")

        prior = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self.cap - self.cleanup_reserve)
        try:
            self.phase("warmup", self.warmup, 0)
            for name, rate in (("idle", 0), ("steady", 5), ("peak", 25)):
                self.phase(name, self.phase_seconds, rate)
            require(deadline - self.clock() > 10, "No bounded report-handoff budget remains")
            self.start_load(max(1, int(deadline - self.clock())), 25)
            response = client.post(
                "/api/recommendation-reports",
                headers=headers,
                json={"workload_id": self.config["workload_id"], "requested_hours": 6},
            )
            require(response.status_code == 201, "One explicit report admission failed; no retry")
            rid = response.json()["id"]
            self.record["report_id"] = rid
            self.save()
            while True:
                response = client.get(f"/api/recommendation-reports/{rid}")
                require(response.status_code == 200, "Selected report read failed")
                report = response.json()
                if report["export_ready"]:
                    break
                self.sleep(2)
            exported = client.get(f"/api/recommendation-reports/{rid}/export")
            require(
                exported.status_code == 200
                and hashlib.sha256(exported.content).hexdigest() == report["export_sha256"],
                "Terminal report export binding failed",
            )
            result = report["result_v2"]
            require(
                self.short
                or (
                    result.get("source") == "live_krr"
                    and result.get("tool_exit_code") == 0
                    and result.get("tool_invocation")
                    and result.get("source_evidence")
                ),
                "Full qualification requires actual pinned KRR and independent source receipts",
            )
            require(
                report["input_identity"] == {**report["input_identity"], **self.initial["identity"]}
                and report["profile"] == self.initial["profile"],
                "Stored report identity/profile differs",
            )
            if result.get("tool_invocation"):
                require(
                    result["tool_invocation"]["metrics_source_sha256"]
                    == self.initial["metrics_attestation"],
                    "Stored report source differs",
                )
            private_write(self.output / "report.json", exported.text)
            self.checkpoint()
            intersections = self.phase_intersections(result.get("window"))
            require(
                self.short
                or all(
                    any(p["phase"] == name and p["seconds"] > 0 for p in intersections)
                    for name in ("idle", "steady", "peak")
                ),
                "Actual report window misses a required measured phase",
            )
            self.record.update(
                {
                    "finished_at": utc(),
                    "elapsed_seconds": self.clock() - start,
                    "export_sha256": report["export_sha256"],
                    "result_state": report["result_state"],
                    "query_window": result.get("window"),
                    "phase_intersections": intersections,
                    "qualified": not self.short and report["result_state"] == "available",
                }
            )
            require(
                self.short or self.record["qualified"],
                "Finite history remains nonavailable; do not extend or retry",
            )
            self.save()
        except BaseException:
            self.record.update({"finished_at": utc(), "failed": True, "qualified": False})
            self.save()
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)
            was_failed = self.record.get("failed", False)
            try:
                self.stop_load()
            except BaseException:
                self.record.update({"failed": True, "qualified": False, "cleanup_failed": True})
                raise
            finally:
                self.record.update(
                    {"cleanup_finished_at": utc(), "elapsed_total_seconds": self.clock() - start}
                )
                over_cap = self.record["elapsed_total_seconds"] > self.cap
                if over_cap:
                    self.record.update({"failed": True, "qualified": False, "cap_exceeded": True})
                self.save()
                if over_cap and not was_failed:
                    raise TimeoutError("Finite recipe cap exceeded during cleanup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run",
        action="store_true",
        help="Run the fixed full recipe; default is read-only readiness",
    )
    mode.add_argument(
        "--short",
        action="store_true",
        help="Run a scaled controlled handoff, never six-hour qualification",
    )
    mode.add_argument(
        "--renew-readers",
        action="store_true",
        help="Renew only existing read identities, without starting services or a cohort",
    )
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    value = config(args.config)
    lab = owned_lab(value)
    lock = (lab.directory / "operation.lock").open("a")
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = lab.directory / "resource-history" / str(uuid4())
    output.mkdir(parents=True, mode=0o700)
    recipe = Recipe(lab, value, output, short=args.short)
    if args.renew_readers:
        recipe.record["reader_renewal"] = lab.renew_readers()
        recipe.save()
        print(json.dumps({"receipt": str(output / "receipt.json"), "cohort_started": False}))
        return
    owned = [recipe.inspect(cid) for cid in value["runtime_containers"]] + lab.ownership()[
        "container"
    ]
    api_port = str(
        urlsplit(value["api_url"]).port or (443 if value["api_url"].startswith("https:") else 80)
    )
    require(
        any(
            binding["HostIp"] == "127.0.0.1" and binding["HostPort"] == api_port
            for container in owned
            if container["State"]["Running"]
            for bindings in container["HostConfig"].get("PortBindings", {}).values()
            for binding in bindings or []
        ),
        "API origin is not published by an owned running container",
    )
    if not (args.run or args.short):
        lab.containment(read_only=True)
        recipe.checkpoint(full=True)
        print(
            json.dumps(
                {"receipt": str(output / "receipt.json"), "ready": True, "cohort_started": False}
            )
        )
        return
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass("Existing Operator password: ")
    )
    verify = ssl.create_default_context(cafile=value["ca_file"]) if value["ca_file"] else True
    with httpx.Client(
        base_url=value["api_url"], verify=verify, trust_env=False, follow_redirects=False, timeout=8
    ) as client:
        response = client.post(
            "/api/login",
            json={"username": value["operator"], "password": password},
            headers={"Origin": value["web_origin"]},
        )
        require(
            response.status_code == 200 and response.json()["principal"]["role"] == "operator",
            "Operator sign-in failed",
        )
        recipe.run(
            client, {"Origin": value["web_origin"], "X-CSRF-Token": response.json()["csrf_token"]}
        )
    print(
        json.dumps(
            {
                "receipt": str(output / "receipt.json"),
                "qualified": recipe.record["qualified"],
                "short_harness_only": args.short,
            }
        )
    )


if __name__ == "__main__":
    main()
