from __future__ import annotations

from sqlalchemy import select

from cloudops.features.knowledge.catalog import index_status, read_document
from cloudops.features.knowledge.index_guard import WriterGuard
from cloudops.features.knowledge.indexing import _start_attempt, index_corpus
from cloudops.features.knowledge.models import KnowledgeDocumentVersion, KnowledgeIndexAttempt
from knowledge_fixtures import document, write_corpus


def test_guard_busy_does_not_start_an_attempt(db, tmp_path):
    write_corpus(tmp_path)
    with WriterGuard():
        result = index_corpus(tmp_path)
    assert result.outcome == "index_busy"
    db.expire_all()
    assert db.scalar(select(KnowledgeIndexAttempt)) is None


def test_failed_update_then_retry_preserves_last_good(db, tmp_path):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    db.expire_all()
    old = db.scalar(select(KnowledgeDocumentVersion))
    (tmp_path / "runbooks/fixture-guide.md").unlink()
    failed = index_corpus(tmp_path)
    assert failed.outcome == "failed"
    db.expire_all()
    assert index_status(db).state == "degraded"
    assert read_document(db, old.id).content

    write_corpus(
        tmp_path,
        [document(version="2")],
        {"fixture-guide": "# Fixture guide\n\nRecovered.\n"},
    )
    recovered = index_corpus(tmp_path)
    assert recovered.outcome == "indexed"
    db.expire_all()
    assert index_status(db).state == "ready"
    assert recovered.generation_id != first.generation_id


def test_interrupted_attempt_is_reconciled_by_next_writer(db, tmp_path):
    write_corpus(tmp_path)
    from cloudops.shared.database import engine

    connection = engine().connect()
    try:
        attempt_id, fence = _start_attempt(connection)
        assert attempt_id and fence == 1
    finally:
        connection.close()
    result = index_corpus(tmp_path)
    assert result.outcome == "indexed"
    db.expire_all()
    attempt = db.get(KnowledgeIndexAttempt, attempt_id)
    assert attempt.outcome == "interrupted"
