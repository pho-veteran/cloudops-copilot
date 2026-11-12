from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from cloudops.features.knowledge import indexing
from cloudops.features.knowledge.catalog import (
    index_status,
    list_documents,
    read_chunk,
    read_document,
)
from cloudops.features.knowledge.index_guard import WriterGuard
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeGeneration,
    KnowledgeGenerationChunk,
)
from cloudops.shared.errors import DomainError
from knowledge_fixtures import document, write_corpus


def _ids(db):
    db.expire_all()
    rows = db.scalars(
        select(KnowledgeDocumentVersion).order_by(KnowledgeDocumentVersion.document_key)
    ).all()
    return rows


def test_empty_not_indexed_then_index_and_repeat(db, tmp_path):
    status = index_status(db)
    assert status.state == "not_indexed"
    assert not status.has_active_generation

    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    assert first.outcome == "indexed"
    db.expire_all()
    status = index_status(db)
    assert status.state == "ready"
    assert status.has_active_generation
    document_id = _ids(db)[0].id
    chunk_id = db.scalar(select(KnowledgeChunk.id))

    second = index_corpus(tmp_path)
    assert second.outcome == "unchanged"
    assert second.generation_id == first.generation_id
    db.expire_all()
    assert _ids(db)[0].id == document_id
    assert db.scalar(select(KnowledgeChunk.id)) == chunk_id
    assert db.scalar(select(KnowledgeGeneration.id)) == first.generation_id


def test_update_retains_old_exact_document_and_chunk(db, tmp_path):
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    db.expire_all()
    old_document = _ids(db)[0]
    old_chunk = db.scalar(
        select(KnowledgeChunk).where(KnowledgeChunk.document_version_id == old_document.id)
    )
    old_content = old_document.content

    write_corpus(
        tmp_path,
        [document(version="2", title="Fixture guide v2")],
        {"fixture-guide": "# Fixture guide v2\n\nChanged body.\n"},
    )
    assert index_corpus(tmp_path).outcome == "indexed"
    db.expire_all()
    assert read_document(db, old_document.id).content == old_content.decode("utf-8")
    assert read_chunk(db, old_chunk.id, old_document.id).content == old_chunk.content.decode(
        "utf-8"
    )
    assert len(_ids(db)) == 2


def test_remove_documents_creates_empty_ready_generation(db, tmp_path):
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    db.expire_all()
    old_id = _ids(db)[0].id
    write_corpus(tmp_path, [])
    result = index_corpus(tmp_path)
    assert result.outcome == "indexed"
    db.expire_all()
    status = index_status(db)
    assert status.state == "ready"
    assert status.active_generation.document_count == 0
    page = list_documents(db, query=None, tags=[], limit=10, cursor=None)
    assert page.items == []
    assert page.catalog_empty_reason == "empty_catalog"
    assert read_document(db, old_id).content


def test_same_version_conflict_keeps_active_generation(db, tmp_path):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    db.expire_all()
    old_id = _ids(db)[0].id
    write_corpus(tmp_path, [document()], {"fixture-guide": "# Fixture guide\n\nChanged.\n"})
    result = index_corpus(tmp_path)
    assert result.outcome == "failed"
    assert "different content" in result.reason
    db.expire_all()
    status = index_status(db)
    assert status.state == "degraded"
    assert status.active_generation.id == first.generation_id
    assert read_document(db, old_id).content


def test_invalid_source_keeps_last_good(db, tmp_path):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    (tmp_path / "runbooks/fixture-guide.md").unlink()
    result = index_corpus(tmp_path)
    assert result.outcome == "failed"
    db.expire_all()
    assert index_status(db).state == "degraded"
    assert index_status(db).active_generation.id == first.generation_id


def test_busy_writer_does_not_capture_or_activate(db, tmp_path):
    write_corpus(tmp_path)
    with WriterGuard():
        result = index_corpus(tmp_path)
    assert result.outcome == "index_busy"
    db.expire_all()
    assert index_status(db).state == "not_indexed"


def test_search_and_tag_filters_use_active_generation(db, tmp_path):
    write_corpus(
        tmp_path,
        [
            document(
                document_key="latency",
                title="Latency guide",
                tags=["latency"],
                source_path="runbooks/latency.md",
            ),
            document(
                document_key="memory",
                title="Memory guide",
                tags=["memory"],
                source_path="runbooks/memory.md",
            ),
        ],
        {
            "latency": "# Latency guide\n\nRequest latency and errors.\n",
            "memory": "# Memory guide\n\nMemory working set and limits.\n",
        },
    )
    index_corpus(tmp_path)
    db.expire_all()
    page = list_documents(db, query="latency", tags=[], limit=10, cursor=None)
    assert [item.document_key for item in page.items] == ["latency"]
    page = list_documents(db, query=None, tags=["memory"], limit=10, cursor=None)
    assert [item.document_key for item in page.items] == ["memory"]
    page = list_documents(db, query="does-not-exist", tags=[], limit=10, cursor=None)
    assert page.items == []
    assert page.catalog_empty_reason == "no_matches"


def test_cursor_is_generation_bound_and_deterministic(db, tmp_path):
    write_corpus(
        tmp_path,
        [
            document(document_key="one", title="One", source_path="runbooks/one.md"),
            document(document_key="two", title="Two", source_path="runbooks/two.md"),
        ],
        {"one": "# One\n\none\n", "two": "# Two\n\ntwo\n"},
    )
    index_corpus(tmp_path)
    db.expire_all()
    first = list_documents(db, query=None, tags=[], limit=1, cursor=None)
    assert first.next_cursor
    second = list_documents(db, query=None, tags=[], limit=1, cursor=first.next_cursor)
    assert len(second.items) == 1
    assert second.items[0].document_key != first.items[0].document_key

    write_corpus(
        tmp_path,
        [
            document(document_key="one", title="One", version="2", source_path="runbooks/one.md"),
            document(document_key="two", title="Two", version="2", source_path="runbooks/two.md"),
        ],
        {"one": "# One\n\none changed\n", "two": "# Two\n\ntwo changed\n"},
    )
    index_corpus(tmp_path)
    db.expire_all()
    with pytest.raises(DomainError) as error:
        list_documents(db, query=None, tags=[], limit=1, cursor=first.next_cursor)
    assert error.value.code == "catalog_changed"


def test_exact_chunk_requires_matching_parent(db, tmp_path):
    write_corpus(tmp_path)
    index_corpus(tmp_path)
    db.expire_all()
    row = db.scalar(select(KnowledgeChunk))
    with pytest.raises(DomainError) as error:
        read_chunk(db, row.id, uuid4())
    assert error.value.code == "chunk_not_found"


def test_unknown_exact_ids_do_not_fallback(db, tmp_path):
    write_corpus(tmp_path)
    index_corpus(tmp_path)
    db.expire_all()
    with pytest.raises(DomainError) as error:
        read_document(db, uuid4())
    assert error.value.code == "document_not_found"


def test_status_uses_real_attempt_evidence(db, tmp_path):
    write_corpus(tmp_path)
    index_corpus(tmp_path)
    db.expire_all()
    status = index_status(db)
    assert status.last_attempt is not None
    assert status.last_success is not None
    assert status.last_attempt.outcome == "indexed"
    assert status.last_attempt.finished_at is not None
    assert status.active_generation.document_count == 1
    assert status.retained.document_versions == 1


def test_chunker_version_change_rebuilds_generation_and_keeps_old_chunks(db, tmp_path, monkeypatch):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    db.expire_all()
    old_document = _ids(db)[0]
    old_chunk = db.scalar(
        select(KnowledgeChunk).where(KnowledgeChunk.document_version_id == old_document.id)
    )
    old_bytes = old_chunk.content

    next_chunker = indexing.CHUNKER_VERSION + "-next"
    monkeypatch.setattr(indexing, "CHUNKER_VERSION", next_chunker)
    rebuilt = index_corpus(tmp_path)
    assert rebuilt.outcome == "indexed"
    assert rebuilt.generation_id != first.generation_id
    db.expire_all()
    assert _ids(db)[0].id == old_document.id
    assert read_chunk(db, old_chunk.id, old_document.id).content == old_bytes.decode("utf-8")
    second = index_status(db)
    assert second.active_generation.chunker_version == next_chunker
    assert db.get(KnowledgeGeneration, rebuilt.generation_id).config["chunker"] == next_chunker


def test_search_config_only_change_rebuilds_generation_and_reuses_chunks(db, tmp_path, monkeypatch):
    write_corpus(
        tmp_path,
        [document(title="Saturation guide")],
        {"fixture-guide": "# Context\n\nBody without the metadata title.\n"},
    )

    def legacy_chunk_vector(document, chunk):
        return indexing._vector(
            chunk.heading_label or "", " / ".join(chunk.heading_path), chunk.content.decode("utf-8")
        )

    # A real projection change, rather than only changing a version string:
    # the previous configuration omitted document titles from chunk vectors.
    with monkeypatch.context() as legacy:
        legacy.setattr(indexing, "SEARCH_CONFIG_VERSION", "legacy-fixture")
        legacy.setattr(indexing, "_chunk_vector", legacy_chunk_vector)
        first = index_corpus(tmp_path)
    db.expire_all()
    old_document = _ids(db)[0]
    old_chunk = db.scalar(
        select(KnowledgeChunk).where(KnowledgeChunk.document_version_id == old_document.id)
    )

    rebuilt = index_corpus(tmp_path)
    assert rebuilt.outcome == "indexed"
    assert rebuilt.generation_id != first.generation_id
    db.expire_all()
    assert _ids(db)[0].id == old_document.id
    assert (
        db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == old_chunk.id)).id
        == old_chunk.id
    )
    assert read_chunk(db, old_chunk.id, old_document.id).content
    matches = select(
        KnowledgeGenerationChunk.search_vector.op("@@")(
            func.websearch_to_tsquery("english", "saturation")
        )
    ).where(KnowledgeGenerationChunk.chunk_id == old_chunk.id)
    assert (
        db.scalar(matches.where(KnowledgeGenerationChunk.generation_id == first.generation_id))
        is False
    )
    assert (
        db.scalar(matches.where(KnowledgeGenerationChunk.generation_id == rebuilt.generation_id))
        is True
    )
    assert first.source_digest == rebuilt.source_digest
    assert (
        db.get(KnowledgeGeneration, first.generation_id).config_digest
        != db.get(KnowledgeGeneration, rebuilt.generation_id).config_digest
    )


def test_search_ranks_title_then_heading_then_body_before_paging(db, tmp_path):
    rows = [
        document(document_key=key, title=title, source_path=f"runbooks/{key}.md")
        for key, title in [
            ("a-body", "Background"),
            ("m-heading", "Notes"),
            ("z-title", "Saturation guide"),
            ("zz-title", "Saturation guide"),
        ]
    ]
    write_corpus(
        tmp_path,
        rows,
        {
            "a-body": "# Background\n\nSaturation.\n",
            "m-heading": "# Notes\n\n## Saturation\n\nContext.\n",
            "z-title": "# Saturation guide\n\nContext.\n",
            "zz-title": "# Saturation guide\n\nContext.\n",
        },
    )
    assert index_corpus(tmp_path).outcome == "indexed"
    first = list_documents(db, query="saturation", tags=[], limit=2, cursor=None)
    assert [row.document_key for row in first.items] == ["z-title", "zz-title"]
    second = list_documents(db, query="saturation", tags=[], limit=2, cursor=first.next_cursor)
    assert [row.document_key for row in second.items] == ["m-heading", "a-body"]
    repeat = list_documents(db, query="saturation", tags=[], limit=2, cursor=None)
    assert repeat == first


def test_all_chunk_projections_include_document_title(db, tmp_path):
    write_corpus(
        tmp_path,
        [document(title="Saturation guide")],
        {
            "fixture-guide": "# Context\n\nFirst paragraph.\n\n## Details\n\nSecond paragraph.\n",
        },
    )
    result = index_corpus(tmp_path)
    assert result.outcome == "indexed"
    matches = db.scalar(
        select(func.count())
        .select_from(KnowledgeGenerationChunk)
        .where(
            KnowledgeGenerationChunk.generation_id == result.generation_id,
            KnowledgeGenerationChunk.search_vector.op("@@")(
                func.websearch_to_tsquery("english", "saturation")
            ),
        )
    )
    assert matches == result.chunk_count == 2


def test_retired_tuple_conflict_and_approved_old_snapshot_reactivation(db, tmp_path):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    old = _ids(db)[0]
    original = old.content
    write_corpus(tmp_path, [document(version="2")], {"fixture-guide": "# Fixture guide\n\nNew.\n"})
    second = index_corpus(tmp_path)
    assert second.outcome == "indexed"
    write_corpus(
        tmp_path, [document()], {"fixture-guide": "# Fixture guide\n\nForged old version.\n"}
    )
    assert index_corpus(tmp_path).outcome == "failed"
    db.expire_all()
    assert index_status(db).active_generation.id == second.generation_id
    assert read_document(db, old.id).content.encode("utf-8") == original
    write_corpus(tmp_path)
    restored = index_corpus(tmp_path)
    assert restored.outcome == "reactivated"
    assert restored.generation_id == first.generation_id


def test_partial_build_failure_rolls_back_all_public_rows(db, tmp_path, monkeypatch):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    original_chunks = indexing._chunk_rows
    rows = [document(document_key=k, source_path=f"runbooks/{k}.md") for k in ("new-a", "new-b")]
    write_corpus(tmp_path, rows)

    def fail_second(session, row, prepared):
        if row.document_key == "new-b":
            raise indexing.KnowledgeFailure("Controlled build failure")
        return original_chunks(session, row, prepared)

    monkeypatch.setattr(indexing, "_chunk_rows", fail_second)
    assert index_corpus(tmp_path).outcome == "failed"
    db.expire_all()
    status = index_status(db)
    assert status.active_generation.id == first.generation_id
    assert status.retained.document_versions == 1
    assert status.retained.generations == 1
    assert len(_ids(db)) == 1
    monkeypatch.setattr(indexing, "_chunk_rows", original_chunks)
    assert index_corpus(tmp_path).outcome == "indexed"
