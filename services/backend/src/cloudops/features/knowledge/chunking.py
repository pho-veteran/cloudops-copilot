"""Deterministic, byte-preserving, heading-aware document chunks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

CHUNKER_VERSION = "2"
MAX_CHUNK_BYTES = 4 * 1024
MAX_CHUNKS = 200
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class ChunkingError(ValueError):
    """A document cannot be represented within the bounded chunk contract."""


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading_path: tuple[str, ...]
    heading_label: str | None
    heading_locator: str
    start_byte: int
    end_byte: int
    content: bytes
    content_sha256: str


def _canonical_heading(path: tuple[str, ...]) -> str:
    return (
        "heading:"
        + hashlib.sha256(
            json.dumps(path, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _utf8_end(data: bytes, start: int, limit: int) -> int:
    end = min(limit, len(data))
    while end > start:
        try:
            data[start:end].decode("utf-8")
            return end
        except UnicodeDecodeError:
            end -= 1
    return min(start + 1, len(data))


def _split_section(
    data: bytes, *, heading_path: tuple[str, ...], heading_label: str | None
) -> list[tuple[bytes, int, int]]:
    pieces: list[tuple[bytes, int, int]] = []
    start = 0
    while start < len(data):
        if len(data) - start <= MAX_CHUNK_BYTES:
            end = len(data)
        else:
            limit = start + MAX_CHUNK_BYTES
            paragraph = data.rfind(b"\n\n", start + 1, limit)
            if paragraph > start:
                end = paragraph + 2
            else:
                end = _utf8_end(data, start, limit)
        if end <= start:
            raise ChunkingError("chunking made no progress")
        pieces.append((data[start:end], start, end))
        start = end
    return pieces


def chunk_document(content: bytes) -> tuple[Chunk, ...]:
    if not content:
        return ()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ChunkingError("document must be valid UTF-8") from None
    lines = text.splitlines(keepends=True)
    sections: list[tuple[bytes, int, tuple[str, ...], str | None]] = []
    stack: list[tuple[int, str]] = []
    section_start = 0
    offset = 0
    fenced = False
    fence_marker = ""
    for line in lines:
        line_bytes = line.encode("utf-8")
        line_without_newline = line.rstrip("\r\n")
        fence = FENCE.match(line_without_newline)
        if fence:
            marker = fence.group(1)
            if fenced:
                if marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                    fenced = False
                    fence_marker = ""
            else:
                fenced = True
                fence_marker = marker
        elif not fenced:
            match = HEADING.match(line_without_newline)
            if match:
                if offset > section_start:
                    sections.append(
                        (
                            content[section_start:offset],
                            section_start,
                            tuple(label for _level, label in stack),
                            stack[-1][1] if stack else None,
                        )
                    )
                level = len(match.group(1))
                label = match.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, label))
                section_start = offset
        offset += len(line_bytes)
    if section_start < len(content):
        sections.append(
            (
                content[section_start:],
                section_start,
                tuple(label for _level, label in stack),
                stack[-1][1] if stack else None,
            )
        )
    chunks: list[Chunk] = []
    for section, section_offset, heading_path, heading_label in sections:
        if not section:
            continue
        label = heading_label[:200] if heading_label else None
        locator = _canonical_heading(heading_path)
        for piece, start, end in _split_section(
            section, heading_path=heading_path, heading_label=heading_label
        ):
            if len(chunks) >= MAX_CHUNKS:
                raise ChunkingError(f"document exceeds the {MAX_CHUNKS}-chunk cap")
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    heading_path=heading_path,
                    heading_label=label,
                    heading_locator=locator,
                    start_byte=section_offset + start,
                    end_byte=section_offset + end,
                    content=piece,
                    content_sha256=hashlib.sha256(piece).hexdigest(),
                )
            )
    if len(chunks) > MAX_CHUNKS:
        raise ChunkingError(f"document exceeds the {MAX_CHUNKS}-chunk cap")
    return tuple(chunks)
