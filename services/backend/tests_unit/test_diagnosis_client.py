from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from cloudops.features.diagnosis.messages import Message
from cloudops.features.diagnosis.profile import ProfileError
from cloudops.features.diagnosis.schemas import CallKind
from cloudops.features.diagnosis.transport import ModelClient
from diagnosis_fake import mock
from diagnosis_fixtures import (
    invalid_envelope,
    profile,
    refusal_payload,
    response,
    text_payload,
    tool_payload,
    truncated_payload,
    usage_invalid,
    usage_missing,
    usage_null,
)

TOKEN = "synthetic-test-token"
MESSAGES = [Message("system", "Be terse."), Message("user", "Reply OK")]


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_TEST_TOKEN", TOKEN)


def _send(profile_overrides=None, payload=None, *, status=200, headers=None, calls=None):
    value = profile(**(profile_overrides or {}))
    seen = calls if calls is not None else []

    def handler(request):
        seen.append(request)
        return response(
            payload if payload is not None else text_payload(), status=status, headers=headers
        )

    result = ModelClient(value, transport=mock(handler)).send(MESSAGES, before_send=lambda: None)
    return result, seen


def test_request_shape_and_text_result():
    result, seen = _send()
    assert result.kind is CallKind.TEXT
    assert result.text == "ok"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 10
    assert result.usage_status == "observed"
    assert result.request_id == "chatcmpl-fixture"
    assert result.served_model == "fixture-served"
    assert result.http_status == 200
    assert result.error_code is None
    assert result.identity.config_id == "test-config"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:18080/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert body == {
        "model": "fixture-model",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Reply OK"},
        ],
        "stream": False,
        "max_tokens": 32,
    }


def test_explicit_reasoning_effort_is_sent_and_bound_to_call_identity():
    result, seen = _send(
        {
            "reasoning_effort": "max",
            "output_limit": {"param": "max_completion_tokens", "value": 1024},
        }
    )
    assert result.kind is CallKind.TEXT
    body = json.loads(seen[0].content)
    assert body["reasoning_effort"] == "max"
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    assert (
        result.identity.behavior_digest
        == profile(
            reasoning_effort="max",
            output_limit={"param": "max_completion_tokens", "value": 1024},
        ).behavior_digest()
    )


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"param": "max_tokens", "value": 7}, {"max_tokens": 7}),
        ({"param": "max_completion_tokens", "value": 7}, {"max_completion_tokens": 7}),
        (
            {"param": "omit", "value": None, "omit_policy_id": "endpoint-policy"},
            {},
        ),
    ],
)
def test_exactly_one_output_limit_parameter(override, expected):
    result, seen = _send({"output_limit": override})
    assert result.kind is CallKind.TEXT
    body = json.loads(seen[0].content)
    assert {key for key in body if key in {"max_tokens", "max_completion_tokens"}} == set(expected)
    for key, value in expected.items():
        assert body[key] == value


@pytest.mark.parametrize(
    "payload,kind,usage_status",
    [
        (text_payload(), CallKind.TEXT, "observed"),
        (usage_missing(), CallKind.TEXT, "unavailable"),
        (usage_null(), CallKind.TEXT, "unavailable"),
        (usage_invalid(), CallKind.TEXT, "invalid"),
        (truncated_payload(), CallKind.TRUNCATED, "observed"),
        (refusal_payload(), CallKind.REFUSAL, "observed"),
        (tool_payload(), CallKind.TOOL_OUTPUT, "observed"),
        (invalid_envelope(), CallKind.INVALID_ENVELOPE, "unavailable"),
    ],
)
def test_normalized_outcomes(payload, kind, usage_status):
    result, _ = _send(payload=payload)
    assert result.kind is kind
    assert result.usage_status == usage_status
    assert result.error_code == (
        None
        if kind in {CallKind.TEXT, CallKind.TRUNCATED, CallKind.REFUSAL, CallKind.TOOL_OUTPUT}
        else kind.value
    )


def test_finish_failed_and_null_are_unsupported():
    for finish in ("failed", None, "weird"):
        payload = text_payload("x", finish=finish)
        result, _ = _send(payload=payload)
        assert result.kind is CallKind.UNSUPPORTED_FINISH
        assert result.error_code == "unsupported_finish"


def test_content_not_string_and_missing_message_are_invalid_shape():
    payload = text_payload()
    payload["choices"][0]["message"]["content"] = [{"type": "text"}]
    result, _ = _send(payload=payload)
    assert result.kind is CallKind.UNSUPPORTED_SHAPE

    payload = text_payload()
    payload["choices"][0].pop("message")
    result, _ = _send(payload=payload)
    assert result.kind is CallKind.INVALID_ENVELOPE


@pytest.mark.parametrize(
    "status,kind",
    [
        (301, CallKind.UNEXPECTED_REDIRECT),
        (302, CallKind.UNEXPECTED_REDIRECT),
        (307, CallKind.UNEXPECTED_REDIRECT),
        (308, CallKind.UNEXPECTED_REDIRECT),
        (400, CallKind.REQUEST_REJECTED),
        (404, CallKind.REQUEST_REJECTED),
        (401, CallKind.AUTH_DENIED),
        (403, CallKind.AUTH_DENIED),
        (500, CallKind.UPSTREAM_ERROR),
        (503, CallKind.UPSTREAM_ERROR),
    ],
)
def test_http_status_mapping(status, kind):
    result, _ = _send(status=status, payload={})
    assert result.kind is kind
    assert result.http_status == status
    assert result.error_code == kind.value


def test_no_content_status_is_invalid_content_type():
    value = profile()
    client = ModelClient(
        value,
        transport=mock(lambda request: httpx.Response(204)),
    )
    result = client.send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INVALID_CONTENT_TYPE


def test_429_retry_after_variants_are_observed_not_retried():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    for headers, expected in [
        (None, None),
        ({"Retry-After": "30"}, 30),
        ({"Retry-After": future}, (25, 35)),
        ({"Retry-After": "0"}, 0),
        ({"Retry-After": "not-a-date"}, None),
        ({"Retry-After": "9" * 200}, 86400),
        ({"Retry-After": "0" * 5000 + "300"}, 300),
        ({"Retry-After": "0" * 5000}, 0),
    ]:
        result, seen = _send(
            status=429,
            payload={
                "error": {"type": "rate_limit_exceeded", "message": "provider detail sk-secret"}
            },
            headers=headers,
            calls=[],
        )
        assert result.kind is CallKind.RATE_LIMITED
        assert result.error_code == "rate_limited"
        assert len(seen) == 1
        if isinstance(expected, tuple):
            assert expected[0] <= result.retry_after_seconds <= expected[1]
        else:
            assert result.retry_after_seconds == expected
        assert "sk-secret" not in repr(result)
        assert result.retry_after_raw is None or len(result.retry_after_raw) <= 64


def test_content_types_and_size_cap():
    for headers, kind in [
        ({"Content-Type": "text/html"}, CallKind.INVALID_CONTENT_TYPE),
        ({"Content-Type": "text/event-stream"}, CallKind.INVALID_CONTENT_TYPE),
        ({"Content-Type": "application/problem+json"}, CallKind.TEXT),
        ({"Content-Type": "application/json; charset=utf-8"}, CallKind.TEXT),
        ({"Content-Type": "application/octet-stream"}, CallKind.INVALID_CONTENT_TYPE),
    ]:
        result, _ = _send(payload=text_payload(), headers=headers)
        assert result.kind is kind
    result, _ = _send(
        payload=text_payload(),
        headers={"Content-Type": "application/json", "Content-Length": "262145"},
    )
    assert result.kind is CallKind.RESPONSE_TOO_LARGE


def test_invalid_json_and_empty_body():
    client = ModelClient(
        profile(),
        transport=mock(
            lambda request: httpx.Response(
                200, content=b"{", headers={"Content-Type": "application/json"}
            )
        ),
    )
    result = client.send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INVALID_ENVELOPE

    client = ModelClient(
        profile(),
        transport=mock(
            lambda request: httpx.Response(
                200, content=b"", headers={"Content-Type": "application/json"}
            )
        ),
    )
    result = client.send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INVALID_ENVELOPE


def test_zero_call_paths(monkeypatch):
    calls = []
    handler = mock(lambda request: calls.append(request) or response(text_payload()))
    disabled = profile(
        mode="disabled",
        endpoint_label=None,
        base_url=None,
        model=None,
        output_limit=None,
        authorization_id=None,
        budget_policy_id=None,
    )
    result = ModelClient(disabled, transport=handler).send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.NOT_CONFIGURED
    assert calls == []

    monkeypatch.delenv("DIAGNOSIS_TEST_TOKEN")
    result = ModelClient(profile(), transport=handler).send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.CREDENTIAL_UNAVAILABLE
    assert calls == []

    monkeypatch.setenv("DIAGNOSIS_TEST_TOKEN", "bad token")
    result = ModelClient(profile(), transport=handler).send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.CREDENTIAL_INVALID
    assert calls == []

    monkeypatch.setenv("DIAGNOSIS_TEST_TOKEN", TOKEN)
    small = profile(limits={"max_input_bytes": 1})
    result = ModelClient(small, transport=handler).send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INPUT_REJECTED
    assert calls == []


def test_recursive_json_is_normalized():
    payload = b"[" * 100_000 + b"]" * 100_000
    client = ModelClient(
        profile(),
        transport=mock(
            lambda request: httpx.Response(
                200, content=payload, headers={"Content-Type": "application/json"}
            )
        ),
    )
    result = client.send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INVALID_ENVELOPE


def test_hook_runs_once_and_propagates_denial():
    calls = []
    handler = mock(lambda request: calls.append(request) or response(text_payload()))
    seen = []
    result = ModelClient(profile(), transport=handler).send(
        MESSAGES, before_send=lambda: seen.append("hook")
    )
    assert result.kind is CallKind.TEXT
    assert seen == ["hook"]

    class Denied(Exception):
        pass

    def deny():
        raise Denied("no")

    before_denial = len(calls)
    with pytest.raises(Denied):
        ModelClient(profile(), transport=handler).send(MESSAGES, before_send=deny)
    assert len(calls) == before_denial


def test_send_inside_event_loop_directs_to_asend():
    client = ModelClient(profile(), transport=mock(lambda request: response(text_payload())))

    async def run():
        with pytest.raises(RuntimeError, match="asend"):
            client.send(MESSAGES, before_send=lambda: None)

    asyncio.run(run())


def test_secret_never_appears_in_result():
    result, _ = _send(
        status=401,
        payload={"error": {"message": f"bad {TOKEN}", "code": "invalid_api_key"}},
    )
    assert TOKEN not in repr(result)
    assert "invalid_api_key" not in repr(result)


def test_transport_and_proxy_cannot_be_combined():
    with pytest.raises(ValueError, match="proxy"):
        ModelClient(
            profile(proxy={"url": "http://127.0.0.1:3128"}),
            transport=mock(lambda request: response(text_payload())),
        )


def test_from_config_path_missing_and_invalid(tmp_path):
    assert ModelClient.from_config_path(None).profile is None
    assert ModelClient.from_config_path(tmp_path / "missing.json").profile is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError):
        ModelClient.from_config_path(tmp_path / "bad.json")


def test_content_filter_and_length_non_string():
    content_filter = text_payload("x", finish="content_filter")
    result, _ = _send(payload=content_filter)
    assert result.kind is CallKind.REFUSAL

    length = text_payload()
    length["choices"][0]["message"]["content"] = None
    length["choices"][0]["finish_reason"] = "length"
    result, _ = _send(payload=length)
    assert result.kind is CallKind.UNSUPPORTED_SHAPE


def test_retry_after_control_text_is_dropped():
    for raw in ("30\x1b]0;x", "0" * 64 + "\x1b]0;x"):
        result, _ = _send(status=429, payload={}, headers={"Retry-After": raw})
        assert result.kind is CallKind.RATE_LIMITED
        assert result.retry_after_seconds is None
        assert result.retry_after_raw is None


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "unsupported"])
def test_compressed_response_is_rejected_without_reading_body(encoding):
    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed response must not be read or decompressed")
            yield b""

    client = ModelClient(
        profile(),
        transport=mock(
            lambda request: httpx.Response(
                200,
                stream=UnreadBody(),
                headers={"Content-Type": "application/json", "Content-Encoding": encoding},
            )
        ),
    )
    result = client.send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.INVALID_ENCODING
    assert result.response_bytes == 0


def test_unusable_ca_returns_config_invalid_without_network():
    calls = []
    value = profile(tls={"ca_file": "missing-ca.pem"})
    result = ModelClient(
        value,
        transport=mock(lambda request: calls.append(request) or response(text_payload())),
    ).send(MESSAGES, before_send=lambda: None)
    assert result.kind is CallKind.CONFIG_INVALID
    assert calls == []


def _capture(status, body, *, headers=None, content_type="application/json"):
    values = {"Content-Type": content_type, **(headers or {})}
    client = ModelClient(
        profile(),
        transport=mock(lambda request: httpx.Response(status, content=body, headers=values)),
        capture_provider_error=True,
    )
    return client.send(MESSAGES, before_send=lambda: None)


def test_provider_error_is_not_captured_by_default():
    result, _ = _send(
        status=400,
        payload={"error": {"message": "bad", "type": "invalid_request_error"}},
    )
    assert result.kind is CallKind.REQUEST_REJECTED
    assert result.provider_error is None


def test_provider_error_fields_and_request_id_are_bounded():
    body = json.dumps(
        {
            "error": {
                "message": "Unsupported value: 'reasoning_effort' does not support 'max'.",
                "type": "invalid_request_error",
                "param": "reasoning_effort",
                "code": "unsupported_value",
            }
        }
    ).encode()
    result = _capture(400, body, headers={"x-request-id": "req_abc123"})
    assert result.kind is CallKind.REQUEST_REJECTED
    assert result.error_code == "request_rejected"
    detail = result.provider_error
    assert detail.type == "invalid_request_error"
    assert detail.code == "unsupported_value"
    assert detail.param == "reasoning_effort"
    assert detail.message.startswith("Unsupported value")
    assert detail.request_id == "req_abc123"
    assert detail.body_bytes_read == len(body) and detail.body_truncated is False
    assert "provider_error" not in repr(result)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_provider_error_is_captured_for_every_rejection_status(status):
    body = json.dumps({"error": {"type": "some_error", "message": "nope"}}).encode()
    result = _capture(status, body)
    assert result.provider_error is not None
    assert result.provider_error.type == "some_error"


def test_provider_error_redacts_credentials_and_contacts():
    message = (
        f"key {TOKEN} rejected; sk-live_ABCDEFGH12345678 and Bearer abc.def; "
        "api_key=xyz contact ops@example.com; " + "Q" * 40
    )
    body = json.dumps(
        {"error": {"message": message, "code": f"x{TOKEN}", "param": "sk-ABCDEFGH1234"}}
    ).encode()
    result = _capture(400, body)
    detail = result.provider_error
    rendered = json.dumps(detail.__dict__)
    for secret in (
        TOKEN,
        "sk-live_ABCDEFGH12345678",
        "abc.def",
        "xyz",
        "ops@example.com",
        "Q" * 40,
    ):
        assert secret not in rendered
    assert detail.code is None and detail.param is None
    assert "[redacted]" in detail.message


def test_provider_error_message_is_truncated_and_control_free():
    body = json.dumps({"error": {"message": "a\nb\x00c " + "word " * 400}}).encode()
    detail = _capture(400, body).provider_error
    assert len(detail.message) <= 512
    assert all(char.isprintable() for char in detail.message)


def test_provider_error_oversized_body_is_truncated_without_parsing():
    body = json.dumps({"error": {"message": "x" * 20000, "type": "big"}}).encode()
    detail = _capture(400, body).provider_error
    assert detail.body_truncated is True
    # One in-memory chunk: it is consumed whole, but only the cap is retained.
    assert detail.body_bytes_read == len(body)
    assert detail.type is None and detail.message is None


@pytest.mark.parametrize("body", [b"", b"not json", b'{"error": "flat string"}', b"[1,2]"])
def test_provider_error_tolerates_empty_and_non_object_bodies(body):
    detail = _capture(400, body, content_type="text/plain").provider_error
    assert detail.type is None and detail.message is None
    assert detail.body_truncated is False
    assert detail.body_bytes_read == len(body)


def test_provider_error_compressed_body_is_not_read():
    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed rejection body must not be read")
            yield b""

    client = ModelClient(
        profile(),
        transport=mock(
            lambda request: httpx.Response(
                400,
                stream=UnreadBody(),
                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            )
        ),
        capture_provider_error=True,
    )
    detail = client.send(MESSAGES, before_send=lambda: None).provider_error
    assert detail.body_truncated is True and detail.body_bytes_read == 0


@pytest.mark.parametrize(
    "fragment",
    [
        "password='tiny-secret'",
        'api_key="tiny-secret"',
        'Bearer "tiny-secret"',
        "key=tiny-secret",
        "token: 'tiny-secret'",
        '"secret": "tiny-secret"',
    ],
)
def test_provider_error_redacts_quoted_and_generic_assignments(fragment):
    body = json.dumps({"error": {"message": f"rejected {fragment} here"}}).encode()
    detail = _capture(400, body).provider_error
    assert "tiny-secret" not in json.dumps(detail.__dict__)
    assert "[redacted]" in detail.message


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False):
        self.chunks = list(chunks)
        self.stall = stall
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.stall:
            await asyncio.sleep(3600)


def _stream_capture(stream, **profile_overrides):
    client = ModelClient(
        profile(**profile_overrides),
        transport=mock(
            lambda request: httpx.Response(
                400, stream=stream, headers={"Content-Type": "application/json"}
            )
        ),
        capture_provider_error=True,
    )
    return client.send(MESSAGES, before_send=lambda: None)


def test_provider_error_reports_consumed_bytes_for_oversized_chunk():
    stream = _CountingStream([b"x" * 9000, b"never"])
    result = _stream_capture(stream)
    assert result.kind is CallKind.REQUEST_REJECTED
    assert result.provider_error.body_bytes_read == 9000
    assert result.provider_error.body_truncated is True
    assert stream.yielded == 1


def test_provider_error_stops_requesting_chunks_at_the_cap():
    stream = _CountingStream([b"y" * 8192, b"z" * 8192])
    result = _stream_capture(stream)
    assert stream.yielded == 1
    assert result.provider_error.body_bytes_read == 8192
    assert result.provider_error.body_truncated is True


def test_provider_error_body_stalling_after_the_cap_keeps_rejection_status():
    stream = _CountingStream([b"y" * 8192], stall=True)
    result = _stream_capture(stream)
    assert result.kind is CallKind.REQUEST_REJECTED and result.http_status == 400


def test_provider_error_body_stalling_before_the_cap_keeps_rejection_status():
    limits = {
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 1,
        "pool_timeout_seconds": 1,
        "deadline_seconds": 1,
    }
    stream = _CountingStream([b'{"error":'], stall=True)
    result = _stream_capture(stream, limits=limits)
    assert result.kind is CallKind.REQUEST_REJECTED and result.http_status == 400
    assert result.provider_error.body_truncated is True
    assert result.provider_error.body_bytes_read == len(b'{"error":')


def test_provider_error_redacts_escaped_quotes_and_keeps_counts():
    message = (
        r'password="prefix\"sensitive-suffix" then Expected tokens: 100; received tokens: 200.'
    )
    detail = _capture(400, json.dumps({"error": {"message": message}}).encode()).provider_error
    assert "sensitive-suffix" not in detail.message and "prefix" not in detail.message
    assert "tokens: 100" in detail.message and "tokens: 200" in detail.message


def test_provider_error_late_in_the_deadline_still_reads_available_body(monkeypatch):
    import cloudops.features.diagnosis.transport as transport_module

    real = transport_module.time.monotonic
    start = real()
    offsets = iter([0.0] + [0.8] * 50)
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: start + next(offsets, 0.8))
    limits = {
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 1,
        "pool_timeout_seconds": 1,
        "deadline_seconds": 1,
    }
    body = json.dumps({"error": {"type": "late_error"}}).encode()
    client = ModelClient(
        profile(limits=limits),
        transport=mock(
            lambda request: httpx.Response(
                400, content=body, headers={"Content-Type": "application/json"}
            )
        ),
        capture_provider_error=True,
    )
    detail = client.send(MESSAGES, before_send=lambda: None).provider_error
    assert detail.type == "late_error" and detail.body_truncated is False
