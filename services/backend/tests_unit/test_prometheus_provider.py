import base64
import json
from pathlib import Path

import pytest

from cloudops.features.investigations import collector, prometheus
from cloudops.features.investigations.prometheus_config import QUERY_IDS

CERT = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
SCOPED_QUERY = (
    'sum(rate(cloudops_http_requests_total{namespace="{{namespace}}",'
    'deployment_uid="{{workload_uid}}"}[1m]))'
)


def write_config(tmp_path):
    path = tmp_path / "prometheus.json"
    path.write_text(
        json.dumps(
            {
                "schema": "PrometheusReadConfig/v1",
                "endpoint_id": "lab-prometheus",
                "producer_version": "prometheus/v3.5.0",
                "server": "https://prometheus.test:8443/prometheus",
                "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
                "step_seconds": 30,
                "binding": {
                    "deployment_uid_label": "deployment_uid",
                    "namespace_label": "namespace",
                },
                "queries": {
                    query_id: {"query": SCOPED_QUERY, "unit": "count"} for query_id in QUERY_IDS
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_provider_registry_composes_only_known_providers(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setenv("PROMETHEUS_READ_CONFIG", str(path))
    prometheus.clear_cache()
    adapters = collector.configured_adapters("prometheus")
    assert set(adapters) == {f"prometheus.{query_id}" for query_id in QUERY_IDS}
    assert all(collector.REGISTERED_ADAPTERS.get(key) is value for key, value in adapters.items())
    assert collector.configured_adapters("none") == {}
    assert set(collector.configured_adapters("development_fixture")) == {"development_fixture"}


@pytest.mark.parametrize(
    "mode",
    [
        "unknown",
        "none,prometheus",
        "development_fixture,prometheus",
        "prometheus,prometheus",
        "prometheus,unknown",
    ],
)
def test_invalid_provider_composition_fails_closed(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PROMETHEUS_READ_CONFIG", str(write_config(tmp_path)))
    prometheus.clear_cache()
    with pytest.raises(ValueError):
        collector.configured_adapters(mode)


def test_prometheus_without_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_READ_CONFIG", raising=False)
    prometheus.clear_cache()
    with pytest.raises(ValueError):
        collector.configured_adapters("prometheus")
