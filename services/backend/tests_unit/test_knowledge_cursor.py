from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest

from cloudops.features.knowledge.catalog import _decode_cursor, _encode_cursor
from cloudops.shared.errors import DomainError


def test_cursor_round_trip():
    generation = uuid4()
    cursor = _encode_cursor(offset=25, generation_id=generation, query="latency", tags=["a"])
    value = _decode_cursor(cursor)
    assert value["offset"] == 25
    assert value["generation_id"] == str(generation)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"v": 2, "offset": 0, "generation_id": str(uuid4()), "q": "a", "tags": "b"},
        {"v": 1, "offset": -1, "generation_id": str(uuid4()), "q": "a", "tags": "b"},
        {"v": 1, "offset": 0, "generation_id": str(uuid4()), "q": "a", "tags": "b", "x": 1},
        {"v": True, "offset": 0, "generation_id": str(uuid4()), "q": "a", "tags": "b"},
        {"v": 1, "offset": True, "generation_id": str(uuid4()), "q": "a", "tags": "b"},
    ],
)
def test_malformed_cursor_is_rejected(payload):
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode("ascii")).decode("ascii")
    with pytest.raises(DomainError) as error:
        _decode_cursor(cursor)
    assert error.value.code == "invalid_cursor"


def test_non_ascii_and_oversized_cursor_are_rejected():
    with pytest.raises(DomainError):
        _decode_cursor("not-base64!")
    with pytest.raises(DomainError):
        _decode_cursor("a" * 513)
