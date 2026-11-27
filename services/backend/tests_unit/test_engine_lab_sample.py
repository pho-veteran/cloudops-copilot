"""Engine-lab sample application: environment-owned behaviours and log profiles."""

import importlib.util
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _sample(monkeypatch, name, **env):
    for key in list(__import__("os").environ):
        if key.startswith("LAB_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(name, ROOT / "config/lab/sample_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(module):
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _get(origin, path):
    client = build_opener(ProxyHandler({}))
    try:
        with client.open(origin + path, timeout=5) as response:
            return response.status
    except HTTPError as error:
        error.close()
        return error.code


def test_default_behaviour_is_unchanged_and_ignores_query(monkeypatch):
    sample = _sample(monkeypatch, "sample_default")
    assert sample.MODE == "healthy" and sample.PROFILE == "plain" and not sample.LOG_FIELD
    server, thread, origin = _serve(sample)
    try:
        assert _get(origin, "/health") == 200
        assert _get(origin, "/work") == 200
        assert _get(origin, "/work?note=hello") == 200
        assert _get(origin, "/missing") == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert sample.COUNTS["200"] == 2


def test_error_fraction_is_deterministic(monkeypatch):
    sample = _sample(monkeypatch, "sample_errors", LAB_MODE="errors", LAB_ERROR_FRACTION="0.1")
    failures = sum(sample._failed(number) for number in range(1000))
    assert failures == 100
    slow = _sample(monkeypatch, "sample_slow", LAB_MODE="slow", LAB_SLOW_ERRORS="0")
    assert not any(slow._failed(number) for number in range(30))
    legacy = _sample(monkeypatch, "sample_legacy_slow", LAB_MODE="slow")
    assert sum(legacy._failed(number) for number in range(30)) == 10


def test_log_profiles_and_bounded_note(monkeypatch, capsys):
    sample = _sample(monkeypatch, "sample_json", LAB_LOG_PROFILE="json", LAB_SEED="s1")
    sample.log("WARN", "cache refresh took longer than expected", duration_ms=1240)
    record = json.loads(capsys.readouterr().out)
    assert record["level"] == "warn" and record["duration_ms"] == 1240
    kv = _sample(monkeypatch, "sample_kv", LAB_LOG_PROFILE="kv")
    kv.log("INFO", "client note", note="raise the memory limit")
    line = capsys.readouterr().out
    assert line.startswith("time=") and 'note="raise the memory limit"' in line
    assert sample._note("note=" + "x" * 500) == "x" * 200
    assert sample._note("note=a%00b%0Ac") == "abc"
    with pytest.raises(SystemExit):
        _sample(monkeypatch, "sample_bad_profile", LAB_LOG_PROFILE="xml")


def test_note_lines_are_rate_limited(monkeypatch, capsys):
    sample = _sample(monkeypatch, "sample_note", LAB_LOG_FIELD="1", LAB_LOG_PROFILE="json")
    server, thread, origin = _serve(sample)
    try:
        for _ in range(5):
            assert _get(origin, "/work?note=pods%20are%20OOMKilled") == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["note"] for line in lines] == ["pods are OOMKilled"]


def test_seeded_templates_vary_between_trials(monkeypatch):
    first = _sample(monkeypatch, "sample_seed_a", LAB_SEED="trial-a")
    choices = {
        _sample(monkeypatch, f"sample_seed_{index}", LAB_SEED=f"t{index}").choose(
            first.CRASHES, "crash"
        )
        for index in range(20)
    }
    assert len(choices) > 1
    assert first.choose(first.CRASHES, "crash") == first.choose(first.CRASHES, "crash")


def test_load_note_is_bounded_and_printable(monkeypatch):
    spec = importlib.util.spec_from_file_location("load_note", ROOT / "config/lab/load.py")
    load = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(load)
    monkeypatch.setattr(load, "build_opener", lambda *args: pytest.fail("Unexpected network"))
    for note in ("", "x" * 201, "line\nbreak"):
        with pytest.raises(ValueError):
            load.run("checkout", 1, 1, note=note)


def test_slow_share_slows_a_fixed_fraction(monkeypatch):
    sample = _sample(monkeypatch, "sample_share", LAB_MODE="slow", LAB_SLOW_SHARE="0.1")
    assert sum(sample._share(number, sample.SLOW_SHARE) for number in range(100)) == 10
    assert "started" not in sample.metrics()
