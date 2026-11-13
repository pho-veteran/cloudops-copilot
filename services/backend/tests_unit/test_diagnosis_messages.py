from __future__ import annotations

import pytest

from cloudops.features.diagnosis.messages import (
    Message,
    MessageError,
    prepare_payload,
    serialize_payload,
)
from diagnosis_fixtures import profile


def _body(content: str = "hello", **overrides):
    return prepare_payload(profile(**overrides), [Message("user", content)])


def test_accepts_message_and_mapping_input():
    assert _body() == _body()
    assert prepare_payload(profile(), [{"role": "user", "content": "hello"}]) == _body()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [Message("user", "a"), Message("user", "b"), Message("user", "c")],
        [Message("user", "a"), Message("system", "b")],
        [Message("system", "a"), Message("system", "b"), Message("user", "c")],
        [{"role": "assistant", "content": "a"}],
        [{"role": "user", "content": 1}],
        [{"role": "user", "content": "a", "extra": 1}],
        "not-a-list",
    ],
)
def test_rejects_invalid_message_shapes(messages):
    with pytest.raises(MessageError):
        prepare_payload(profile(), messages)


def test_rejects_control_characters():
    with pytest.raises(MessageError):
        prepare_payload(profile(), [Message("user", "bad\x00content")])
    assert prepare_payload(profile(), [Message("user", "line\nbreak\tok")])


def test_rejects_unencodable_surrogate():
    with pytest.raises(MessageError):
        prepare_payload(profile(), [Message("user", "bad\ud800")])


def test_bounds_serialized_bytes_including_multibyte():
    with pytest.raises(MessageError):
        prepare_payload(profile(limits={"max_input_bytes": 80}), [Message("user", "é" * 100)])
    prepared = prepare_payload(
        profile(limits={"max_input_bytes": 1024}), [Message("user", "é" * 100)]
    )
    assert "é".encode() in prepared


def test_serialize_payload_is_the_transport_measurement():
    messages = [Message("system", "s"), Message("user", "u")]
    encoded = serialize_payload(profile(), messages)
    assert encoded == prepare_payload(profile(), messages)
    assert encoded.startswith(b'{"model":')


def test_prepare_payload_still_enforces_max_input_bytes():
    tiny = profile(limits={"max_input_bytes": 16})
    with pytest.raises(MessageError):
        prepare_payload(tiny, [Message("user", "x" * 64)])
