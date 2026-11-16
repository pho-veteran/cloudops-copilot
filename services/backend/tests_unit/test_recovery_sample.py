"""Real sample HTTP counters and bounded-loader inputs; hardware OOM/capacity qualify separately."""

import importlib
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def sample(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "config/lab"))
    application = importlib.import_module("recovery_app")
    metrics = importlib.import_module("sample_app")
    with metrics.LOCK:
        metrics.COUNTS.clear()
        metrics.TOTAL_SECONDS = 0
    server = application.ThreadingHTTPServer(("127.0.0.1", 0), application.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield application, metrics, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_recovery_metrics_come_from_actual_work_requests(sample):
    _, metrics, origin = sample
    client = build_opener(ProxyHandler({}))
    with client.open(origin + "/health", timeout=2) as response:
        assert response.status == 200
    assert "lab_http_request_duration_seconds_count 0" in metrics.metrics()
    with client.open(origin + "/work", timeout=2) as response:
        assert response.status == 200 and response.read() == b"completed\n"
    with client.open(origin + "/metrics", timeout=2) as response:
        text = response.read().decode()
    assert 'lab_http_requests_total{status="200"} 1' in text
    assert 'lab_http_requests_total{status="503"} 0' in text
    assert "lab_http_request_duration_seconds_count 1" in text
    assert metrics.TOTAL_SECONDS >= 0.08


def test_saturated_capacity_returns_and_records_real_503(sample):
    app, metrics, origin = sample
    assert app.SLOTS.acquire(blocking=False) and app.SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(HTTPError) as caught:
            build_opener(ProxyHandler({})).open(origin + "/work", timeout=2)
        assert caught.value.code == 503
        caught.value.close()
    finally:
        app.SLOTS.release()
        app.SLOTS.release()
    assert 'lab_http_requests_total{status="503"} 1' in metrics.metrics()
    assert metrics.TOTAL_SECONDS >= 0.15


@pytest.mark.parametrize("variant", ["bad-image", "unsupported"])
def test_fault_image_cannot_serve_or_allocate(sample, monkeypatch, variant):
    app, _, _ = sample
    monkeypatch.setenv("E07_SAMPLE_VARIANT", variant)
    with pytest.raises(SystemExit):
        app.main()
    assert app.WORKING_SET is None


@pytest.mark.parametrize("concurrency", [0, 17, True])
def test_load_concurrency_is_bounded_before_any_transport(monkeypatch, concurrency):
    monkeypatch.syspath_prepend(str(ROOT / "config/lab"))
    load = importlib.import_module("load")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Invalid load input cannot reach transport.")

    monkeypatch.setattr(load, "build_opener", unexpected)
    with pytest.raises(ValueError):
        load.run("checkout", 1, 1, concurrency=concurrency)


def test_load_reuses_transport_setup_for_the_finite_trial(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "config/lab"))
    load = importlib.import_module("load")
    setups, requests = [], []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _maximum):
            return b"completed"

    class Opener:
        def open(self, url, *, timeout):
            requests.append((url, timeout))
            return Response()

    def setup(_handler):
        setups.append(True)
        return Opener()

    monkeypatch.setattr(load, "build_opener", setup)
    result = load.run("checkout", 1, 3, concurrency=16)
    assert setups == [True]
    assert len(requests) == 3 and result["responses"] == {"200": 3}
