from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/backend/tests"))
from diagnosis_fake import mock  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "probe_diagnosis_connection", ROOT / "scripts/probe-diagnosis-connection.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

SECRET = "synthetic-probe-secret-value"


def _profile(tmp_path, **overrides):
    value = {
        "schema": "DiagnosisModelProfile/v1",
        "config_id": "probe-config",
        "config_version": "1",
        "mode": "disabled",
        "endpoint_label": "loopback",
        "base_url": "http://127.0.0.1:18080/v1",
        "allow_insecure_loopback": True,
        "model": "fixture-model",
        "reasoning_effort": "max",
        "credential": {"kind": "env", "name": "PROBE_TOKEN"},
        "output_limit": {"param": "max_completion_tokens", "value": 4096},
    }
    value.update(overrides)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    secret = tmp_path / "secret.env"
    secret.write_text(f"PROBE_TOKEN={SECRET}\n")
    return path, secret


def _args(tmp_path, profile, secret, **overrides):
    values = [
        "--profile",
        str(profile),
        "--secret-env",
        str(secret),
        "--reservation-dir",
        str(tmp_path / "reservation"),
        "--authorization-id",
        "owner-probe",
        "--budget-policy-id",
        "one-request",
        "--ceiling-usd",
        "0.10",
        "--input-usd-per-mtok",
        "0.10",
        "--output-usd-per-mtok",
        "0.50",
        "--rates-date",
        "2026-11-24",
    ]
    for key, value in overrides.items():
        values[values.index("--" + key.replace("_", "-")) + 1] = value
    return probe.parser().parse_args(values)


def _transport(status, payload, calls, headers=None):
    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    return mock(handler)


def test_success_writes_private_receipts_with_estimate(tmp_path):
    profile, secret = _profile(tmp_path)
    calls = []
    payload = {
        "id": "chatcmpl-1",
        "model": "fixture-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 300, "total_tokens": 312},
    }
    receipt = probe.run(_args(tmp_path, profile, secret), transport=_transport(200, payload, calls))
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["reasoning_effort"] == "max" and body["max_completion_tokens"] == 4096
    assert receipt["compatibility_pass"] is True and receipt["model_matches"] is True
    assert receipt["estimated_usd"] == pytest.approx((12 * 0.10 + 300 * 0.50) / 1_000_000)
    assert receipt["billed_usd"] is None and receipt["rca_qualified"] is False
    directory = tmp_path / "reservation"
    for name in ("send-reserved.json", "receipt.json"):
        path = directory / name
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 or sys.platform == "win32"
        assert SECRET not in path.read_text()


def test_rejection_keeps_redacted_provider_detail(tmp_path):
    profile, secret = _profile(tmp_path)
    calls = []
    error = {
        "error": {
            "message": f"Unsupported parameter for key {SECRET}",
            "type": "invalid_request_error",
            "param": "reasoning_effort",
            "code": "unsupported_parameter",
        }
    }
    receipt = probe.run(
        _args(tmp_path, profile, secret),
        transport=_transport(400, error, calls, headers={"x-request-id": "req_1"}),
    )
    assert receipt["kind"] == "request_rejected" and receipt["http_status"] == 400
    detail = receipt["provider_error"]
    assert detail["param"] == "reasoning_effort" and detail["request_id"] == "req_1"
    assert SECRET not in json.dumps(receipt)
    assert receipt["estimated_usd"] is None and receipt["compatibility_pass"] is False


def test_existing_reservation_is_never_retried(tmp_path):
    profile, secret = _profile(tmp_path)
    (tmp_path / "reservation").mkdir()
    calls = []
    with pytest.raises(probe.ProbeRefused):
        probe.run(_args(tmp_path, profile, secret), transport=_transport(200, {}, calls))
    assert calls == []


def test_reservation_above_ceiling_is_refused_before_sending(tmp_path):
    profile, secret = _profile(
        tmp_path, output_limit={"param": "max_completion_tokens", "value": 300000}
    )
    calls = []
    with pytest.raises(probe.ProbeRefused):
        probe.run(_args(tmp_path, profile, secret), transport=_transport(200, {}, calls))
    assert calls == [] and not (tmp_path / "reservation").exists()


def test_missing_secret_is_refused(tmp_path):
    profile, secret = _profile(tmp_path)
    secret.write_text("OTHER=1\n")
    with pytest.raises(probe.ProbeRefused):
        probe.run(_args(tmp_path, profile, secret), transport=_transport(200, {}, []))


@pytest.mark.parametrize(
    "override",
    [
        {"ceiling_usd": "nan"},
        {"ceiling_usd": "inf"},
        {"ceiling_usd": "0"},
        {"input_usd_per_mtok": "nan"},
        {"output_usd_per_mtok": "-1"},
        {"output_usd_per_mtok": "inf"},
    ],
)
def test_invalid_cost_inputs_never_send(tmp_path, override):
    profile, secret = _profile(tmp_path)
    calls = []
    with pytest.raises(probe.ProbeRefused):
        probe.run(
            _args(tmp_path, profile, secret, **override), transport=_transport(200, {}, calls)
        )
    assert calls == [] and not (tmp_path / "reservation").exists()


def test_relative_ca_resolves_against_profile_directory(tmp_path, monkeypatch):
    import cloudops.features.diagnosis.profile as profile_module

    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "root-ca.pem").write_text("placeholder")
    path, secret = _profile(directory, tls={"ca_file": "root-ca.pem"})
    seen = []

    def context(*, cafile=None, **kwargs):
        seen.append(Path(cafile))
        return profile_module.ssl.SSLContext(profile_module.ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(profile_module.ssl, "create_default_context", context)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    payload = {
        "id": "chatcmpl-1",
        "model": "fixture-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    probe.run(_args(tmp_path, path, secret), transport=_transport(200, payload, []))
    assert seen and all(item.resolve().parent == directory.resolve() for item in seen)
