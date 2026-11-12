from __future__ import annotations

import hashlib

import pytest

from cloudops.features.knowledge.chunking import (
    MAX_CHUNK_BYTES,
    MAX_CHUNKS,
    ChunkingError,
    chunk_document,
)


def test_heading_aware_chunks_preserve_exact_bytes():
    content = (
        b"# Root\r\n\r\nintro\r\n\r\n"
        b"## First\r\n\r\nfirst body\r\n\r\n"
        b"### Nested\r\n\r\nnested body\r\n"
    )
    chunks = chunk_document(content)
    assert b"".join(chunk.content for chunk in chunks) == content
    assert chunks[0].heading_path == ("Root",)
    assert chunks[1].heading_path == ("Root", "First")
    assert chunks[2].heading_path == ("Root", "First", "Nested")
    assert chunks[0].start_byte == 0
    assert chunks[-1].end_byte == len(content)
    assert all(
        chunk.content_sha256 == hashlib.sha256(chunk.content).hexdigest() for chunk in chunks
    )
    assert len({chunk.heading_locator for chunk in chunks}) == 3


def test_fenced_hash_lines_are_not_headings():
    content = b"# Root\n\n```\n# not a heading\n```\n\nbody\n"
    chunks = chunk_document(content)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ("Root",)


def test_mixed_fence_markers_do_not_close_code_early():
    content = b"# Root\n\n```\n~~~\n# not a heading\n~~~\n```\n\n# Real\n\nbody\n"
    chunks = chunk_document(content)
    assert [chunk.heading_path for chunk in chunks] == [("Root",), ("Real",)]


def test_empty_document_has_no_chunks():
    assert chunk_document(b"") == ()


def test_long_section_splits_at_bounded_utf8_boundaries():
    content = b"# Root\n\n" + ("paragraph line\n\n" * 800).encode("utf-8")
    chunks = chunk_document(content)
    assert len(chunks) > 1
    assert b"".join(chunk.content for chunk in chunks) == content
    assert all(len(chunk.content) <= MAX_CHUNK_BYTES for chunk in chunks)
    assert all(chunk.end_byte > chunk.start_byte for chunk in chunks)


def test_multibyte_character_is_not_cut():
    content = b"# Root\n\n" + ("é" * 3000).encode("utf-8")
    chunks = chunk_document(content)
    assert b"".join(chunk.content for chunk in chunks) == content
    for chunk in chunks:
        chunk.content.decode("utf-8")


def test_chunk_cap_is_enforced():
    content = b"".join(
        f"## Section {index}\n\nbody\n\n".encode("utf-8") for index in range(MAX_CHUNKS + 1)
    )
    with pytest.raises(ChunkingError, match="chunk cap"):
        chunk_document(content)


def test_invalid_utf8_is_rejected():
    with pytest.raises(ChunkingError, match="UTF-8"):
        chunk_document(b"# Root\n\n\xff")


def test_skipped_heading_levels_keep_real_parent_hierarchy():
    content = b"# Root\n### First\na\n### Peer\nb\n## Mid\nc\n#### Deep\nd\n### Next\ne\n"
    chunks = chunk_document(content)
    assert [chunk.heading_path for chunk in chunks] == [
        ("Root",),
        ("Root", "First"),
        ("Root", "Peer"),
        ("Root", "Mid"),
        ("Root", "Mid", "Deep"),
        ("Root", "Mid", "Next"),
    ]
    assert b"".join(chunk.content for chunk in chunks) == content
