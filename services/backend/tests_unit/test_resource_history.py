"""Finite recipe guards; controlled clocks are not real history qualification."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    not (ROOT / "scripts/probe-resource-history.py").is_file(),
    reason="Host-only recipe is tested from a source checkout",
)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    values = []
    for path in ("probe-resource-history.py", "resource_history_source.py"):
        spec = importlib.util.spec_from_file_location(path, ROOT / "scripts" / path)
        value = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(value)
        values.append(value)
    return values


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1/secret",
        "https://localhost?key=secret",
        "http://user@localhost",
    ],
)
def test_only_loopback_origins_without_transmitted_credentials_or_paths(modules, url):
    with pytest.raises((RuntimeError, ValueError)):
        modules[0].local_origin(url)


def test_raw_traffic_keeps_zero_and_only_expected_uid_whitelisted_fields(modules):
    source = modules[1]
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {
                        "pod_uid": "expected",
                        "status": "200",
                        "private_annotation": "omit",
                    },
                    "values": [[100, "0"], [105, "0"]],
                },
                {"metric": {"pod_uid": "foreign"}, "values": [[100, "900"]]},
            ],
        },
    }
    rows = source.traffic_rows(payload, {"expected"}, 110)
    assert rows == [
        {"labels": {"pod_uid": "expected", "status": "200"}, "samples": [[100, "0"], [105, "0"]]}
    ]


@pytest.mark.parametrize(
    "points",
    [[[100, "NaN"]], [[100, "-1"]], [[1, "0"]], [[110, "2"], [105, "3"]], [[100, "5"], [105, "4"]]],
)
def test_invalid_stale_reordered_or_reset_traffic_cannot_prove_a_phase(modules, points):
    with pytest.raises(modules[1].ReadinessFailure):
        modules[1].traffic_rows(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {"pod_uid": "expected"}, "values": points}],
                },
            },
            {"expected"},
            250,
        )


def test_missing_current_uid_measurement_is_not_zero_traffic(modules):
    with pytest.raises(modules[1].ReadinessFailure, match="coverage is missing"):
        modules[1].traffic_rows(
            {"status": "success", "data": {"resultType": "matrix", "result": []}}, {"expected"}, 110
        )


def test_query_envelope_records_actual_phase_intersections(modules, tmp_path):
    recipe = modules[0].Recipe(None, {}, tmp_path)
    recipe.record["phases"] = [
        {
            "name": "idle",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T02:00:00+00:00",
        },
        {
            "name": "steady",
            "started_at": "2026-01-01T02:00:00+00:00",
            "finished_at": "2026-01-01T04:00:00+00:00",
        },
        {
            "name": "peak",
            "started_at": "2026-01-01T04:00:00+00:00",
            "finished_at": "2026-01-01T06:00:00+00:00",
        },
    ]
    assert recipe.phase_intersections(
        {"start": "2026-01-01T00:05:00Z", "end": "2026-01-01T06:05:00Z"}
    ) == [
        {"phase": "idle", "seconds": 6900},
        {"phase": "steady", "seconds": 7200},
        {"phase": "peak", "seconds": 7200},
    ]
    assert recipe.cap == 22080
    assert modules[0].Recipe(None, {}, tmp_path, short=True).cap == 138


def test_phase_boundary_takes_one_checkpoint_without_consuming_the_handoff_budget(
    modules, tmp_path, monkeypatch
):
    clock, calls = [0], []
    recipe = modules[0].Recipe(
        None,
        {},
        tmp_path,
        short=True,
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(recipe, "checkpoint", lambda **kw: calls.append(clock[0]))
    recipe.phase("idle", 15, 0)
    assert calls == [15] and clock[0] == 15


@pytest.mark.parametrize("mode", ["short", "missing_native", "cleanup_failure"])
def test_handoff_never_qualifies_short_missing_native_or_failed_cleanup(
    modules, tmp_path, monkeypatch, mode
):
    driver = modules[0]
    clock = [0]
    identity = {"uid": "original"}
    profile = {"history_hours": 6}
    report = {
        "id": "stored",
        "input_identity": identity,
        "profile": profile,
        "export_ready": True,
        "result_state": "available",
        "result_v2": {"window": None},
        "export_sha256": driver.hashlib.sha256(b"stored bytes").hexdigest(),
    }
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/export"):
            return httpx.Response(200, content=b"stored bytes")
        return httpx.Response(201 if request.method == "POST" else 200, json=report)

    lab = SimpleNamespace(containment=lambda **kw: None)
    recipe = driver.Recipe(
        lab, {"workload_id": "owned"}, tmp_path, short=mode == "short", clock=lambda: clock[0]
    )
    monkeypatch.setattr(
        recipe,
        "checkpoint",
        lambda **kw: setattr(
            recipe,
            "initial",
            {"identity": identity, "profile": profile, "metrics_attestation": "controlled"},
        ),
    )
    monkeypatch.setattr(recipe, "start_load", lambda seconds, rate: None)
    monkeypatch.setattr(recipe, "stop_load", lambda: None)
    if mode == "cleanup_failure":
        report["result_v2"].update(
            {
                "source": "live_krr",
                "tool_exit_code": 0,
                "tool_invocation": {"metrics_source_sha256": "controlled"},
                "source_evidence": {"fixture": True},
            }
        )
        monkeypatch.setattr(
            recipe,
            "phase_intersections",
            lambda window: [{"phase": name, "seconds": 1} for name in ("idle", "steady", "peak")],
        )

        def failed_cleanup():
            raise RuntimeError("Controlled cleanup failure")

        monkeypatch.setattr(recipe, "stop_load", failed_cleanup)

    def phase(name, seconds, rate):
        clock[0] += seconds

    monkeypatch.setattr(recipe, "phase", phase)
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
        if mode == "short":
            recipe.run(client, {})
        else:
            with pytest.raises(
                RuntimeError,
                match="actual pinned KRR"
                if mode == "missing_native"
                else "Controlled cleanup failure",
            ):
                recipe.run(client, {})
    assert requests.count(("POST", "/api/recommendation-reports")) == 1
    assert not recipe.record["qualified"]
    if mode != "missing_native":
        assert (tmp_path / "report.json").read_bytes() == b"stored bytes"
    if mode == "cleanup_failure":
        assert recipe.record["cleanup_failed"]


def test_expired_source_deadline_exits_before_backend_imports_or_reads(modules):
    driver = modules[0]
    result = driver.subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/resource_history_source.py"),
            "unused",
            "--deadline",
            str(driver.time.time() - 1),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:9/unused_history_test",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 124 and result.stdout == ""


def test_cap_exhaustion_prevents_report_admission_and_preserves_failed_receipt(
    modules, tmp_path, monkeypatch
):
    driver = modules[0]
    clock = [0]
    calls = []
    recipe = driver.Recipe(
        SimpleNamespace(containment=lambda **kw: None),
        {},
        tmp_path,
        short=True,
        clock=lambda: clock[0],
    )
    monkeypatch.setattr(recipe, "checkpoint", lambda **kw: None)
    monkeypatch.setattr(recipe, "phase", lambda *args: clock.__setitem__(0, recipe.cap + 1))
    monkeypatch.setattr(recipe, "stop_load", lambda: calls.append("stopped"))
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda r: pytest.fail("No admission after cap")),
    ) as client:
        with pytest.raises(RuntimeError, match="budget remains"):
            recipe.run(client, {})
    assert calls == ["stopped"] and json.loads((tmp_path / "receipt.json").read_text())["failed"]


def test_wall_clock_alarm_interrupts_blocked_phase_and_cleans_up(modules, tmp_path, monkeypatch):
    driver = modules[0]
    calls = []
    recipe = driver.Recipe(SimpleNamespace(containment=lambda **kw: None), {}, tmp_path, short=True)
    recipe.cap = 20.05
    monkeypatch.setattr(recipe, "checkpoint", lambda **kw: None)
    monkeypatch.setattr(recipe, "phase", lambda *args: driver.time.sleep(1))
    monkeypatch.setattr(recipe, "stop_load", lambda: calls.append("stopped"))
    with pytest.raises(TimeoutError, match="cap reached"):
        recipe.run(None, {})
    assert calls == ["stopped"] and recipe.record["failed"]
    assert driver.signal.getitimer(driver.signal.ITIMER_REAL) == (0, 0)


def test_worker_ownership_is_verified_before_any_source_execution(modules, tmp_path, monkeypatch):
    driver = modules[0]
    recipe = driver.Recipe(None, {"worker_container": "foreign"}, tmp_path, short=True)
    monkeypatch.setattr(recipe, "inspect", lambda cid: driver.require(False, "foreign worker"))
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *args, **kw: pytest.fail("No foreign-container read")
    )
    with pytest.raises(RuntimeError, match="foreign worker"):
        recipe.checkpoint()


def test_cleanup_finds_owned_load_when_launch_acknowledgement_was_lost(modules, tmp_path):
    removed = []

    def command(args, **kwargs):
        if args[1] == "ps":
            return "late-id"
        if args[1] == "inspect":
            return json.dumps([{"Config": {"Labels": {"io.cloudops.e08.cohort": tmp_path.name}}}])
        if args[1] == "rm":
            removed.append(args[-1])
            return ""
        pytest.fail("Unexpected cleanup command")

    recipe = modules[0].Recipe(SimpleNamespace(command=command), {}, tmp_path, short=True)
    assert recipe.load_id is None
    recipe.stop_load()
    assert removed == ["late-id"]


def test_different_producer_or_uid_snapshot_cannot_be_stitched_into_success(
    modules, tmp_path, monkeypatch
):
    driver = modules[0]
    source = {
        "identity": {"uid": "original", "namespace": "demo", "name": "checkout"},
        "profile": {"image": "pinned"},
        "metrics_attestation": "source",
        "pods": [{"uid": "pod"}],
        "measurements": {
            "cpu": [{"samples": [[driver.time.time(), "1"]]}],
            "memory": [{"samples": [[driver.time.time(), "1"]]}],
        },
    }
    lab = SimpleNamespace(
        credentials_valid=lambda **kw: None,
        directory=tmp_path,
        env={},
        marker={"deployments": {"checkout": "original"}},
        command=lambda *args, **kw: json.dumps(source),
    )
    for name in ("reader", "monitoring"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "expiry.json").write_text('{"expires_at":"2099-01-01T00:00:00Z"}')
    recipe = driver.Recipe(
        lab, {"worker_container": "owned", "workload_id": "owned"}, tmp_path, short=True
    )
    monkeypatch.setattr(recipe, "footprint", lambda: {"containers": []})
    monkeypatch.setattr(recipe, "inspect", lambda cid: None)
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *args, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(source)),
    )
    recipe.checkpoint()
    changed = copy.deepcopy(source)
    changed["profile"]["image"] = "different"
    source.update(changed)
    with pytest.raises(RuntimeError, match="start a new cohort"):
        recipe.checkpoint()
