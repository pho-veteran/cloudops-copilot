import base64
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cloudops.features.investigations.prometheus_config import (
    MAX_CONFIG_BYTES,
    QUERY_IDS,
    PrometheusConfig,
    escape_promql,
    load_config,
    server_base,
    substitute_query,
)

CERT = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"
SCOPED_QUERY = (
    'sum(rate(cloudops_http_requests_total{namespace="{{namespace}}",'
    'deployment_uid="{{workload_uid}}"}[1m]))'
)


def queries():
    return {query_id: {"query": SCOPED_QUERY, "unit": "count"} for query_id in QUERY_IDS}


def config():
    return {
        "schema": "PrometheusReadConfig/v1",
        "endpoint_id": "lab-prometheus",
        "producer_version": "prometheus/v3.5.0",
        "server": "https://prometheus.test:8443/prometheus",
        "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
        "token": "secret-sentinel",
        "step_seconds": 30,
        "binding": {
            "deployment_uid_label": "deployment_uid",
            "namespace_label": "namespace",
        },
        "queries": queries(),
    }


def test_valid_config_normalizes_server_and_builds_query_url():
    value = PrometheusConfig.model_validate(config())
    assert value.server == "https://prometheus.test:8443/prometheus"
    assert value.query_url() == "https://prometheus.test:8443/prometheus/api/v1/query_range"
    assert set(value.queries) == set(QUERY_IDS)
    assert "secret-sentinel" not in value.model_dump_json(exclude={"token"})


@pytest.mark.parametrize(
    "server",
    [
        "http://prometheus.test:8443",
        "https://user:password@prometheus.test:8443",
        "https://prometheus.test:8443/?query=1",
        "https://prometheus.test:8443/#",
        "https://prometheus.test:8443/../",
        "https://prometheus.test:8443/%2e%2e/",
        "https://prometheus.test:8443\\evil",
        "https://prometheus.test:0",
        "https://prometheus.test:65536",
        "https://prometheus.test:8443/prometheus?",
    ],
)
def test_reject_ambiguous_server(server):
    value = config()
    value["server"] = server
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(value)


@pytest.mark.parametrize(
    "server,expected",
    [
        ("https://prometheus.test/", "https://prometheus.test"),
        ("https://prometheus.test:443/prometheus/", "https://prometheus.test:443/prometheus"),
        ("https://[::1]:8443/prom/path", "https://[::1]:8443/prom/path"),
    ],
)
def test_server_prefix_is_fixed(server, expected):
    assert server_base(server) == expected


@pytest.mark.parametrize(
    "ca",
    [
        "",
        base64.b64encode(b"garbage").decode(),
        "not-base64",
        base64.b64encode(b"-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----").decode(),
    ],
)
def test_reject_invalid_ca(ca):
    value = config()
    value["ca_data"] = ca
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(value)


@pytest.mark.parametrize("step", [29, 301, 0, -1])
def test_step_bounds(step):
    value = config()
    value["step_seconds"] = step
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(value)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_query",
        "extra_query",
        "unsupported_placeholder",
        "missing_namespace",
        "missing_uid",
        "bad_unit",
        "same_binding",
        "bad_binding",
        "bad_schema",
        "bad_endpoint",
        "bad_producer",
    ],
)
def test_reject_invalid_query_or_binding(mutation):
    value = config()
    if mutation == "missing_query":
        del value["queries"]["request_rate"]
    elif mutation == "extra_query":
        value["queries"]["unknown"] = {"query": SCOPED_QUERY, "unit": "count"}
    elif mutation == "unsupported_placeholder":
        value["queries"]["request_rate"]["query"] = 'sum(rate(metric{pod="{{pod_regex}}"}[1m]))'
    elif mutation == "missing_namespace":
        value["queries"]["request_rate"]["query"] = (
            'sum(rate(metric{deployment_uid="{{workload_uid}}"}[1m]))'
        )
    elif mutation == "missing_uid":
        value["queries"]["request_rate"]["query"] = (
            'sum(rate(metric{namespace="{{namespace}}"}[1m]))'
        )
    elif mutation == "bad_unit":
        value["queries"]["request_rate"]["unit"] = "line\nbreak"
    elif mutation == "same_binding":
        value["binding"]["namespace_label"] = "deployment_uid"
    elif mutation == "bad_binding":
        value["binding"]["deployment_uid_label"] = "bad-label"
    elif mutation == "bad_schema":
        value["schema"] = "PrometheusReadConfig/v2"
    elif mutation == "bad_endpoint":
        value["endpoint_id"] = "Bad_Endpoint"
    else:
        value["producer_version"] = "line\nbreak"
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(value)


def test_substitute_escapes_provider_values():
    assert escape_promql('a"b\\c\n') == 'a\\"b\\\\c\\n'
    resolved = substitute_query(SCOPED_QUERY, 'demo"x', "uid\\y")
    assert 'namespace="demo\\"x"' in resolved
    assert 'deployment_uid="uid\\\\y"' in resolved
    assert "{{" not in resolved


def test_load_config_sanitizes_failures(tmp_path):
    path = tmp_path / "read.json"
    path.write_text('{"token": "private-token-sentinel"}', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert "private-token-sentinel" not in str(error.value)
    path.write_text(
        json.dumps({**config(), "server": "http://private-token-sentinel"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert "private-token-sentinel" not in str(error.value)


def test_load_config_rejects_oversized_file_before_json_validation(tmp_path):
    path = tmp_path / "read.json"
    data = json.dumps(config()).encode("utf-8")
    bounded = data.ljust(MAX_CONFIG_BYTES, b" ")
    path.write_bytes(bounded)
    assert load_config(path).endpoint_id == "lab-prometheus"
    path.write_bytes(bounded + b" ")
    with pytest.raises(ValueError, match="configuration is invalid or unavailable"):
        load_config(path)


def test_load_config_sanitizes_excessive_json_nesting(tmp_path):
    path = tmp_path / "read.json"
    path.write_bytes(("[" * 10_000 + "]" * 10_000).encode("ascii"))
    with pytest.raises(ValueError, match="configuration is invalid or unavailable"):
        load_config(path)
