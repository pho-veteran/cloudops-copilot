"""Canonical OpenAI-compatible envelopes and profile fixtures shared by #69/#70 tests."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cloudops.features.diagnosis.profile import ModelProfile


def profile(**overrides: Any) -> ModelProfile:
    value = {
        "schema": "DiagnosisModelProfile/v1",
        "config_id": "test-config",
        "config_version": "1",
        "mode": "evaluation_only",
        "endpoint_label": "loopback",
        "base_url": "http://127.0.0.1:18080/v1",
        "allow_insecure_loopback": True,
        "model": "fixture-model",
        "credential": {"kind": "env", "name": "DIAGNOSIS_TEST_TOKEN"},
        "output_limit": {"param": "max_tokens", "value": 32},
        "authorization_id": "test-owner",
        "budget_policy_id": "one-call",
    }
    value.update(overrides)
    return ModelProfile.model_validate(value)


def text_payload(
    content: str = "ok",
    *,
    finish: str = "stop",
    usage: dict | None = None,
    **extra: Any,
) -> dict:
    payload = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "model": "fixture-served",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    payload.update(extra)
    return payload


def usage_missing() -> dict:
    value = text_payload()
    value.pop("usage")
    return value


def usage_null() -> dict:
    return text_payload(usage=None) | {"usage": None}


def usage_invalid() -> dict:
    return text_payload(usage={"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3})


def refusal_payload() -> dict:
    value = text_payload(content="")
    value["choices"][0]["message"]["refusal"] = "I cannot help with that."
    return value


def truncated_payload() -> dict:
    return text_payload("partial", finish="length")


def tool_payload() -> dict:
    value = text_payload(content=None, finish="tool_calls")
    value["choices"][0]["message"]["tool_calls"] = [{"id": "call-1", "type": "function"}]
    return value


def invalid_envelope() -> dict:
    return {"id": "x", "choices": []}


def response(payload: Any, *, status: int = 200, headers: dict | None = None) -> httpx.Response:
    body = json.dumps(payload)
    values = {"Content-Type": "application/json", **({} if headers is None else headers)}
    return httpx.Response(status, content=body.encode(), headers=values)
