"""Reusable conformant Knowledge corpus fixtures for unit and PostgreSQL tests."""

from __future__ import annotations

import json
from pathlib import Path


def document(**overrides):
    row = {
        "document_key": "fixture-guide",
        "version": "1",
        "title": "Fixture guide",
        "source_path": "runbooks/fixture-guide.md",
        "guide_kind": "diagnosis",
        "tags": ["fixture", "test"],
        "applicability": {
            "kind": "Deployment",
            "symptom_families": [],
            "recovery_templates": [],
        },
        "provenance": {
            "source_revision": "6c8eea37e982f3dc6de2755e7a482eb16aaf1f16",
            "reviewed_at": "2026-11-11",
        },
    }
    row.update(overrides)
    return row


def write_corpus(root: Path, documents=None, contents=None):
    if documents is None:
        documents = [document()]
    contents = contents or {}
    (root / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "runbooks/catalog.json").write_text(
        json.dumps({"schema_version": "RunbookCatalog/v1", "documents": documents}),
        encoding="utf-8",
    )
    for row in documents:
        path = root / row["source_path"]
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            contents.get(
                row["document_key"],
                f"# {row['title']}\n\nFixture body.\n",
            ).encode("utf-8")
        )
    return documents


def one_document_root(tmp_path: Path, **overrides):
    row = document(**overrides)
    write_corpus(tmp_path, [row])
    return tmp_path, row
