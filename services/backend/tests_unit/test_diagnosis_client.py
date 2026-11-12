from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from cloudops.features.diagnosis.messages import Message, MessageError
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
    assert result.error_code == (None if kind is CallKind.TEXT else kind.value)


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
        ({"Retry-After": "9" * 200}, int("9" * 64)),
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
    with pytest.raises(MessageError):
        ModelClient(small, transport=handler).send(MESSAGES, before_send=lambda: None)
    assert calls == []


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
