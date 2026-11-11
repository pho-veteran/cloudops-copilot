from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from cloudops.features.knowledge.catalog import index_status, read_document
from cloudops.features.knowledge.index_guard import GuardLost, WriterGuard
from cloudops.features.knowledge.indexing import (
    _activate,
    _prepare,
    _start_attempt,
    index_corpus,
)
from cloudops.features.knowledge.models import KnowledgeDocumentVersion, KnowledgeIndexAttempt
from cloudops.shared.database import engine
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


def test_failed_update_keeps_last_checked_source_digest(db, tmp_path):
    write_corpus(tmp_path)
    first = index_corpus(tmp_path)
    db.expire_all()
    before = index_status(db)
    (tmp_path / "runbooks/fixture-guide.md").unlink()
    failed = index_corpus(tmp_path)
    assert failed.outcome == "failed"
    db.expire_all()
    after = index_status(db)
    assert after.last_checked_source_digest == first.source_digest
    assert after.last_checked_source_digest == before.last_checked_source_digest


def test_status_marks_an_unfinished_attempt_as_degraded(db, tmp_path):
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    connection = engine().connect()
    try:
        _start_attempt(connection)
    finally:
        connection.close()
    db.expire_all()
    status = index_status(db)
    assert status.state == "degraded"
    assert status.last_attempt.outcome is None
    assert status.last_attempt.reason == "Index attempt did not finish; retry explicitly."


def test_guard_loss_prevents_late_publish(db, tmp_path):
    write_corpus(tmp_path)
    guard = WriterGuard()
    guard.__enter__()
    try:
        attempt_id, fence = _start_attempt(guard.active)
        captured, prepared = _prepare(tmp_path)
        with engine().begin() as killer:
            killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": guard.pid})
        winner = index_corpus(tmp_path)
        assert winner.outcome == "indexed"
        with pytest.raises((GuardLost, SQLAlchemyError)):
            _activate(
                guard.active,
                guard=guard,
                captured=captured,
                prepared=prepared,
                attempt_id=attempt_id,
                fence=fence,
            )
        db.expire_all()
        status = index_status(db)
        assert status.active_generation.id == winner.generation_id
        attempt = db.get(KnowledgeIndexAttempt, attempt_id)
        assert attempt.outcome == "interrupted"
    finally:
        guard.release()


def test_interrupted_attempt_is_reconciled_by_next_writer(db, tmp_path):
    write_corpus(tmp_path)
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
