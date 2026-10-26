import base64
import io
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from pydantic import ValidationError

from cloudops.features.workloads.scope_config import (
    LiveScope,
    NoRedirect,
    ScopeReader,
    Target,
    cluster_fingerprint,
    load_scope,
    server_identity,
)
from cloudops.shared.errors import DomainError

CERT = Path(__file__).resolve().parents[1] / "tests/fixtures/scope-ca.crt"


def connection():
    return {
        "server": "https://cluster.example:6443",
        "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
        "token": "secret-sentinel",
    }


def policy(fingerprint=None):
    return {
        "schema_version": 2,
        "cluster": {
            "key": "lab",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": fingerprint or cluster_fingerprint(connection()),
        },
        "deployments": [{"namespace": "demo", "name": "checkout"}],
    }


@pytest.mark.parametrize(
    "server,expected",
    [
        ("https://CLUSTER.EXAMPLE/", "https://cluster.example:443"),
        ("https://cluster.example:443", "https://cluster.example:443"),
        ("https://cluster.example:6443", "https://cluster.example:6443"),
        ("https://[::1]/", "https://[::1]:443"),
    ],
)
def test_server_normalization(server, expected):
    assert server_identity(server) == expected


@pytest.mark.parametrize(
    "server",
    [
        "http://cluster",
        "https://u:p@cluster",
        "https://cluster/path",
        "https://cluster?",
        "https://cluster#",
        "https://cluster:0",
        "https://cluster:65536",
        "https://cluster/../",
        "https://cluster\n",
        "https://[fe80::1%eth0]",
        "https://cluster\\evil",
        "https://cluster:",
        "https://cluster..example",
        "https://cluster.-example",
    ],
)
def test_reject_ambiguous_server(server):
    with pytest.raises(ValueError):
        server_identity(server)


def test_fingerprint_uses_der_and_normalized_url_not_token_or_pem_wrapping():
    config = connection()
    first = cluster_fingerprint(config)
    config["token"] = "different-token"
    config["server"] = "https://CLUSTER.EXAMPLE:6443/"
    config["ca_data"] = base64.b64encode(CERT.read_bytes().replace(b"\n", b"\r\n")).decode()
    assert cluster_fingerprint(config) == first
    config["server"] = "https://different.example:6443"
    assert cluster_fingerprint(config) != first


@pytest.mark.parametrize(
    "pem",
    [
        b"garbage",
        b"",
        CERT.read_bytes() * 2,
        CERT.read_bytes() + b"extra",
        b"-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----",
    ],
)
def test_reject_invalid_or_multiple_certificates(pem):
    config = {**connection(), "ca_data": base64.b64encode(pem).decode()}
    with pytest.raises((ValueError, OSError)):
        cluster_fingerprint(config)


def test_legacy_and_v2_files_are_distinct(tmp_path):
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(policy()))
    scope, digest = load_scope(path)
    assert isinstance(scope, LiveScope) and len(digest) == 64
    path.write_text('[{"cluster":"fixture","namespace":"demo","name":"old","uid":"fixture-uid"}]')
    entries, other = load_scope(path)
    assert entries[0].source == "development_fixture" and digest != other
    path.write_text("[]")
    assert load_scope(path)[0] == []


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "overlimit",
        "mixed",
        "uid",
        "endpoint",
        "badnamespace",
        "badname",
        "fingerprint",
        "version",
    ],
)
def test_policy_validation(change, tmp_path):
    value = policy()
    if change in {"duplicate", "overlimit"}:
        value["deployments"] *= 2 if change == "duplicate" else 101
    elif change == "mixed":
        value["deployments"][0]["source"] = "development_fixture"
    elif change in {"uid", "endpoint"}:
        value["deployments"][0][change] = "forbidden"
    elif change == "badnamespace":
        value["deployments"][0]["namespace"] = "../demo"
    elif change == "badname":
        value["deployments"][0]["name"] = "Bad_Name"
    elif change == "fingerprint":
        value["cluster"]["expected_fingerprint"] = "A" * 64
    else:
        value["schema_version"] = 3
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError):
        load_scope(path)


def make_reader(tmp_path, config=None, fingerprint=None):
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(config or connection()))
    return ScopeReader(LiveScope.model_validate(policy(fingerprint)), path)


def test_wrong_pin_fails_before_lookup_and_sanitizes_config(tmp_path):
    with pytest.raises(DomainError) as error:
        make_reader(tmp_path, fingerprint="0" * 64)
    assert error.value.code == "cluster_identity_mismatch"
    assert "secret-sentinel" not in error.value.message
    with pytest.raises(DomainError) as error:
        make_reader(tmp_path, {**connection(), "ca_data": "secret-sentinel"})
    assert "secret-sentinel" not in error.value.message


@pytest.mark.parametrize(
    "status,state,reason",
    [
        (404, "missing", "not_found"),
        (403, "unavailable", "permission_denied"),
        (401, "unavailable", "cluster_unavailable"),
        (500, "unavailable", "cluster_unavailable"),
        (302, "unavailable", "cluster_unavailable"),
    ],
)
def test_lookup_classifies_http_errors(tmp_path, status, state, reason):
    reader = make_reader(tmp_path)

    class Opener:
        def open(self, request, timeout):
            assert request.method == "GET" and timeout <= 8
            raise HTTPError(request.full_url, status, "private-provider-sentinel", {}, None)

    reader.opener = Opener()
    result = reader.lookup(Target(namespace="demo", name="checkout"), time.monotonic() + 30)
    assert (result.state, result.reason) == (state, reason)
    assert "private-provider-sentinel" not in repr(result)


@pytest.mark.parametrize(
    "case",
    [
        "present",
        "wrong_name",
        "wrong_namespace",
        "missing_uid",
        "oversized",
        "malformed",
        "timeout",
        "budget",
    ],
)
def test_lookup_identity_and_bounds(tmp_path, case):
    reader = make_reader(tmp_path)
    value = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"uid": "real-uid", "namespace": "demo", "name": "checkout"},
    }
    if case == "wrong_name":
        value["metadata"]["name"] = "other"
    if case == "wrong_namespace":
        value["metadata"]["namespace"] = "other"
    if case == "missing_uid":
        del value["metadata"]["uid"]

    class Opener:
        def open(self, request, timeout):
            assert (
                request.full_url
                == "https://cluster.example:6443/apis/apps/v1/namespaces/demo/deployments/checkout"
            )
            if case == "timeout":
                raise URLError("private-provider-sentinel")
            if case == "budget":
                pytest.fail("Exhausted budget must not perform network reads")
            return io.BytesIO(
                b"x" * 2_000_001
                if case == "oversized"
                else b"invalid"
                if case == "malformed"
                else json.dumps(value).encode()
            )

    reader.opener = Opener()
    result = reader.lookup(
        Target(namespace="demo", name="checkout"),
        time.monotonic() + (-1 if case == "budget" else 30),
    )
    assert result.state == ("present" if case == "present" else "unavailable")
    assert result.uid == ("real-uid" if case == "present" else None)


def test_redirects_never_forward_reader_credentials():
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None
