#!/usr/bin/env python3
"""Generate labelled engine-loop cases on the owned engine lab.

Per trial: purge namespace events, wait for the previous window to close (gap), start a
bounded load, record a baseline, inject one fault, wait for actual fault evidence, collect
through the normal API and worker (diagnosis stays disabled), export RawEvidenceBundle/v1,
reset the fault and purge events again. Derived variants (log noise, instruction text,
a disabled source) are written beside their trial and inherit its split.

Run with CLOUDOPS_LAB_PROJECT=cloudops-engine-lab. Cases are dev-lane, non-qualifying.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "cloudops-engine-lab"
LAB_DIR = Path.home() / ".local/state/cloudops-copilot/engine-loop/lab"
if os.environ.get("CLOUDOPS_LAB_PROJECT") != PROJECT:
    raise SystemExit(f"Set CLOUDOPS_LAB_PROJECT={PROJECT}.")
sys.path.insert(0, str(ROOT / "scripts"))

from engine_loop import lab as cases  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "manage_local_lab", ROOT / "scripts/manage-local-lab.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

KINDS = ("crashloop", "oom", "traffic", "pending", "imagepull", "init-retry")
BASELINE_SECONDS = 60
DWELL_SECONDS = 60
GAP_SECONDS = 45
LOAD_RATE = 5


def now() -> datetime:
    return datetime.now(timezone.utc)


def plan(name: str) -> list[dict]:
    """Trials in a fixed interleaved order so a partial batch still covers every kind."""
    if name == "smoke":
        return [{"kind": kind, "variant": "clean"} for kind in KINDS] + [
            {"kind": None, "variant": "healthy"}
        ]
    if name == "b":
        # Families without derived log variants (pending, imagepull) and the lost crashloop.
        trials = []
        for variant in (
            "clean",
            "event-spam",
            "clean",
            "shifted-window",
            "unrelated-restart",
            "clean",
        ):
            for kind in ("pending", "imagepull"):
                trials.append({"kind": kind, "variant": variant})
        trials += [
            {"kind": "crashloop", "variant": "clean", "derive": ["benign-logs", "injection-logs"]},
            {"kind": "crashloop", "variant": "event-spam"},
            {"kind": None, "variant": "healthy"},
            {"kind": "oom", "variant": "event-spam"},
            {"kind": "traffic", "variant": "unrelated-restart"},
        ]
        return trials
    if name != "m0":
        raise SystemExit(f"unknown plan {name}")
    rounds = [
        "clean",
        "event-spam",
        "clean",
        "shifted-window",
        "unrelated-restart",
        "clean",
        "clean",
        "shifted-window",
    ]
    trials = []
    for index, variant in enumerate(rounds):
        for kind in KINDS:
            derive = []
            if variant == "clean":
                derive = ["benign-logs", "injection-logs"] if index < 3 else ["benign-logs"]
                if index in {0, 2} and kind in {"crashloop", "oom", "traffic"}:
                    derive.append("source-disabled")
            trials.append({"kind": kind, "variant": variant, "derive": derive})
        trials.append(
            {"kind": None, "variant": "healthy", "derive": ["injection-logs"] if index < 4 else []}
        )
        if index in {1, 4, 7}:
            trials.append({"kind": "crashloop", "variant": "out-of-scope"})
    return trials


class Generator:
    def __init__(self, out: Path, batch: str, password: str):
        self.out = out
        self.batch = batch
        self.password = password
        self.lab = driver.Lab(LAB_DIR)
        self.lab.ownership()
        if self.lab.marker is None:
            raise SystemExit("Start the engine lab first.")
        self.state_path = out / "state.json"
        self.state = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {"next": 1, "last_window_end": None, "done": []}
        )
        self.revision = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    def kubectl(self, *args, body=None, timeout=60) -> str:
        return self.lab.kube(*args, body=body, timeout=timeout)

    def purge_events(self) -> dict:
        before = json.loads(self.kubectl("get", "events", "-n", "demo", "-o", "json"))["items"]
        if before:
            self.kubectl("delete", "events", "--all", "-n", "demo", timeout=120)
        after = json.loads(self.kubectl("get", "events", "-n", "demo", "-o", "json"))["items"]
        return {"deleted": len(before), "remaining": len(after), "at": now().isoformat()}

    def start_load(self, case_id: str, target: str, seconds: int) -> str:
        name = f"{PROJECT}-engine-load-{case_id}"[:63]
        subprocess.Popen(
            [
                *self.lab.compose,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--name",
                name,
                "load",
                "--target",
                target,
                "--seconds",
                str(seconds),
                "--rate",
                str(LOAD_RATE),
            ],
            cwd=ROOT,
            env=self.lab.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return name

    def stop_load(self, name: str) -> None:
        subprocess.run(
            ["docker", "stop", "--time", "3", name],
            env=self.lab.env,
            capture_output=True,
            timeout=30,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if not self.lab.active_loads():
                    return
            except RuntimeError:
                # A --rm container can vanish between listing and inspection; retry.
                pass
            time.sleep(2)

    def event_spam(self, deployment_uid: str) -> int:
        stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        history = self.lab.deployment("history")
        items = []
        for index in range(12):
            target = (
                ("Deployment", "checkout", deployment_uid)
                if index % 2 == 0
                else ("Deployment", "history", history["metadata"]["uid"])
            )
            items.append(
                {
                    "apiVersion": "v1",
                    "kind": "Event",
                    "metadata": {
                        "name": f"engine-noise-{time.time_ns()}-{index}",
                        "namespace": "demo",
                    },
                    "involvedObject": {
                        "apiVersion": "apps/v1",
                        "kind": target[0],
                        "name": target[1],
                        "namespace": "demo",
                        "uid": target[2],
                    },
                    "reason": ("ConfigurationReloaded", "CacheWarmed", "SidecarHeartbeat")[
                        index % 3
                    ],
                    "message": "Routine background maintenance completed normally",
                    "type": "Normal",
                    "count": 1,
                    "firstTimestamp": stamp,
                    "lastTimestamp": stamp,
                    "source": {"component": "engine-lab-noise"},
                }
            )
        self.kubectl("apply", "-f", "-", body={"apiVersion": "v1", "kind": "List", "items": items})
        return len(items)

    def wait_running_restart(self, kind: str, seconds: int = 170) -> bool:
        """Crash-type faults: collect 1.5-4 s after a restarted instance starts. The kubelet
        keeps one dead instance per container, so the previous log is readable only while
        a restarted instance runs (measured on this lab: unreadable during back-off), and
        the new instance needs a moment to write its first current log lines."""
        field = "initContainerStatuses" if kind == "init-retry" else "containerStatuses"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pods = json.loads(
                self.kubectl("get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json")
            )["items"]
            for pod in pods:
                for status in pod.get("status", {}).get(field, []) or []:
                    running = status.get("state", {}).get("running") or {}
                    started = running.get("startedAt")
                    if status.get("restartCount", 0) > 0 and started:
                        age = (
                            now() - datetime.fromisoformat(started.replace("Z", "+00:00"))
                        ).total_seconds()
                        if 1.5 <= age <= 4:
                            return True
            time.sleep(0.5)
        return False

    def unrelated_restart(self) -> None:
        self.kubectl("delete", "pod", "-n", "demo", "-l", "app=history", "--wait=false")

    def trial(self, spec: dict) -> dict:
        kind, variant = spec["kind"], spec["variant"]
        number = self.state["next"]
        case_id = f"{kind or 'healthy'}-{variant}-{self.batch}{number:02d}".replace(
            "healthy-healthy", "healthy"
        )
        if variant == "out-of-scope":
            case_id = f"scope-{kind}-{self.batch}{number:02d}"
        record = {"case_id": case_id, "kind": kind, "variant": variant, "steps": {}}
        require = driver.require
        require(not (LAB_DIR / "fault.json").exists(), "A recorded fault exists; reset first.")
        purge = self.purge_events()
        record["steps"]["purge_before"] = purge
        last = self.state.get("last_window_end")
        if last:
            wait = (
                datetime.fromisoformat(last) + timedelta(seconds=GAP_SECONDS) - now()
            ).total_seconds()
            if wait > 0:
                time.sleep(wait)
        receipts = sorted((LAB_DIR / "receipts").glob("*reset-private.json"))
        cleanup = {
            "case_id": case_id,
            "purge": purge,
            "previous_window_end": last,
            "gap_seconds": GAP_SECONDS,
            "previous_reset_receipt_sha256": (
                hashlib.sha256(receipts[-1].read_bytes()).hexdigest() if receipts else None
            ),
            "at": now().isoformat(),
        }
        cleanup_bytes = (json.dumps(cleanup, sort_keys=True) + "\n").encode()
        receipt_path = self.out / "receipts" / f"{case_id}-cleanup.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path.write_bytes(cleanup_bytes)
        cleanup_sha = hashlib.sha256(cleanup_bytes).hexdigest()
        cleanup_at = datetime.fromisoformat(cleanup["at"])
        target = "history" if variant == "out-of-scope" else "checkout"
        load = self.start_load(case_id, "both" if target == "history" else "checkout", 420)
        injected = False
        try:
            start = now()
            time.sleep(BASELINE_SECONDS)
            checkout = self.lab.deployment("checkout")
            inject_at = now()
            if kind:
                self.lab.fault(kind)
                injected = True
            if variant == "event-spam":
                record["steps"]["noise_events"] = self.event_spam(checkout["metadata"]["uid"])
            if variant == "unrelated-restart":
                self.unrelated_restart()
                record["steps"]["unrelated_restart"] = "history pod deleted"
            if kind:
                self.lab.observe()
                record["steps"]["observed_at"] = now().isoformat()
            time.sleep(DWELL_SECONDS)
            if kind in {"crashloop", "init-retry", "oom"}:
                record["steps"]["running_restart"] = self.wait_running_restart(kind)
            end = now()
            window_start = (
                inject_at - timedelta(seconds=10) if variant == "shifted-window" else start
            )
            api = cases.Api(LAB_DIR, self.password)
            try:
                workload = api.workloads()[target]
                ids = api.create_run(workload, window_start, end)
                run = api.wait_run(ids)
                value = cases.export(
                    api,
                    ids,
                    run,
                    workload,
                    case_id=case_id,
                    lab_project=PROJECT,
                    source_revision=self.revision,
                    variant=variant,
                    reset_receipt_sha256=cleanup_sha,
                )
            finally:
                api.close()
        finally:
            self.stop_load(load)
            if injected or (LAB_DIR / "fault.json").exists():
                self.lab.reset()
            record["steps"]["purge_after"] = self.purge_events()
        self.state["last_window_end"] = end.isoformat()
        stale = cases.stale_events(value, cleanup_at)
        label_kind = None if variant in {"out-of-scope", "healthy"} else kind
        rationale = (
            f"Lab trial {case_id}: fault {kind or 'none'} injected on checkout, variant {variant}; "
            f"run target {target}. Label from the injected fault and bundle coverage."
        )
        meta = {
            "trial_id": case_id,
            "kind": kind,
            "family": cases.FAMILY_BY_KIND.get(label_kind, "unknown") if label_kind else "unknown",
            "variant": variant,
            "target": target,
            "window": {"start": window_start.isoformat(), "end": end.isoformat()},
            "inject_at": inject_at.isoformat() if kind else None,
            "run_state": run["state"],
            "contaminated": stale > 0,
            "stale_events": stale,
            "cleanup_receipt_sha256": cleanup_sha,
            "steps": record["steps"],
        }
        cases.write_case(self.out, value, cases.label(value, label_kind, rationale=rationale), meta)
        written = [case_id]
        for derived in spec.get("derive", []):
            if derived == "source-disabled":
                slot = "prometheus.error_ratio" if kind == "traffic" else "kubernetes.logs.current"
                child = cases.derive_disabled(value, f"{case_id}-nosrc", slot)
                child_label = cases.label(
                    child, None, rationale=f"Derived from {case_id}: {slot} disabled; abstain."
                )
            else:
                suffix = "noise" if derived == "benign-logs" else "inject"
                child = cases.derive_logs(value, f"{case_id}-{suffix}", derived)
                if child is None:
                    continue
                child_label = cases.label(
                    child,
                    label_kind,
                    rationale=f"Derived from {case_id}: {derived} added to retained logs.",
                )
            cases.write_case(
                self.out,
                child,
                child_label,
                {**meta, "variant": f"derived:{derived}", "trial_id": case_id},
            )
            written.append(child.case_id)
        self.state["next"] = number + 1
        self.state["done"].append({"spec": spec, "cases": written, "stale_events": stale})
        self.save()
        return {
            "trial": case_id,
            "cases": written,
            "stale_events": stale,
            "run_state": run["state"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plan", default="smoke")
    parser.add_argument("--batch", default="a")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    password = (LAB_DIR / "operator-password").read_text().strip()
    generator = Generator(args.out, args.batch, password)
    trials = plan(args.plan)
    start = len(generator.state["done"])
    todo = trials[start:]
    if args.limit:
        todo = todo[: args.limit]
    failures = 0
    with (LAB_DIR / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generator.lab.ownership()
        for spec in todo:
            try:
                result = generator.trial(spec)
                failures = 0
                print(json.dumps(result), flush=True)
            except Exception as error:  # noqa: BLE001 - recorded, then bounded
                failures += 1
                print(
                    json.dumps({"failed": spec, "error": str(error)[:300]}),
                    flush=True,
                )
                traceback.print_exc()
                if "containment" in str(error).lower() or failures >= 3:
                    print(json.dumps({"stopped": "containment or repeated trial failure"}))
                    return 1
                if (LAB_DIR / "fault.json").exists():
                    generator.lab.reset()
                generator.state["done"].append(
                    {"spec": spec, "cases": [], "error": str(error)[:300]}
                )
                generator.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
