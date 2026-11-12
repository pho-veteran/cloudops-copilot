from __future__ import annotations

import base64
import json
from uuid import uuid4

from sqlalchemy import select

from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.knowledge.models import KnowledgeChunk, KnowledgeDocumentVersion
from cloudops.shared.database import sessions
from knowledge_fixtures import document, write_corpus

ORIGIN = "http://localhost:5173"


def _headers(client, username="viewer"):
    response = client.post(
        "/api/login",
        json={"username": username, "password": "local-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def _ids():
    with sessions()() as db:
        document_row = db.scalar(select(KnowledgeDocumentVersion).limit(1))
        chunk_row = db.scalar(select(KnowledgeChunk).limit(1))
        return document_row.id, chunk_row.id


def test_empty_catalog_is_explicit_and_protected(client):
    headers = _headers(client)
    status = client.get("/api/knowledge/index-status", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "not_indexed"
    assert status.json()["read_available"] is False

    page = client.get("/api/knowledge/documents", headers=headers)
    assert page.status_code == 200
    assert page.json()["items"] == []
    assert page.json()["generation_id"] is None
    assert page.json()["catalog_empty_reason"] == "not_indexed"


def test_anonymous_and_expired_cannot_read(client):
    assert client.get("/api/knowledge/documents").status_code == 401
    assert client.get("/api/knowledge/index-status").status_code == 401
    assert client.get(f"/api/knowledge/documents/{uuid4()}").status_code == 401


def test_both_roles_read_catalog_exact_document_and_chunk(client, tmp_path):
    write_corpus(tmp_path)
    assert index_corpus(tmp_path).outcome == "indexed"
    document_id, chunk_id = _ids()
    for username in ("viewer", "operator"):
        headers = _headers(client, username)
        page = client.get("/api/knowledge/documents", headers=headers)
        assert page.status_code == 200, page.text
        assert page.json()["has_active_generation"] is True
        assert str(page.json()["items"][0]["id"]) == str(document_id)

        detail = client.get(f"/api/knowledge/documents/{document_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["content"].startswith("# Fixture guide")
        assert detail.json()["id"] == str(document_id)
        assert detail.json()["applicability"] == document()["applicability"]
        assert detail.json()["provenance"] == document()["provenance"]

        chunk = client.get(
            f"/api/knowledge/chunks/{chunk_id}",
            params={"document_version_id": str(document_id)},
            headers=headers,
        )
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["content"]
        assert chunk.json()["document_version_id"] == str(document_id)


def test_filters_cursor_and_exact_mismatch_are_typed(client, tmp_path):
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
            "latency": "# Latency guide\n\nlatency body\n",
            "memory": "# Memory guide\n\nmemory body\n",
        },
    )
    index_corpus(tmp_path)
    headers = _headers(client)
    page = client.get(
        "/api/knowledge/documents",
        params=[("q", "latency"), ("limit", "1")],
        headers=headers,
    )
    assert page.status_code == 200
    assert [item["document_key"] for item in page.json()["items"]] == ["latency"]
    assert page.json()["next_cursor"] is None

    first = client.get("/api/knowledge/documents", params={"limit": 1}, headers=headers)
    assert first.status_code == 200
    second = client.get(
        "/api/knowledge/documents",
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]

    document_id, chunk_id = _ids()
    mismatch = client.get(
        f"/api/knowledge/chunks/{chunk_id}",
        params={"document_version_id": str(uuid4())},
        headers=headers,
    )
    assert mismatch.status_code == 404
    assert mismatch.json()["error"]["code"] == "chunk_not_found"

    invalid = client.get(
        "/api/knowledge/documents", params={"cursor": "not-base64"}, headers=headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_cursor"


def test_index_status_reports_ready_after_activation(client, tmp_path):
    write_corpus(tmp_path)
    index_corpus(tmp_path)
    headers = _headers(client)
    status = client.get("/api/knowledge/index-status", headers=headers)
    assert status.status_code == 200
    body = status.json()
    assert body["state"] == "ready"
    assert body["has_active_generation"] is True
    assert body["active_generation"]["document_count"] == 1
    assert body["last_attempt"]["outcome"] == "indexed"
    assert "source_path" not in body


def test_malformed_cursor_is_typed_at_the_api_and_failed_source_reason_is_safe(client, tmp_path):
    headers = _headers(client)
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "v": 1,
                "offset": 0,
                "generation_id": [],
                "q": "a",
                "tags": "b",
            }
        ).encode("ascii")
    ).decode("ascii")
    response = client.get("/api/knowledge/documents", params={"cursor": cursor}, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"
    write_corpus(tmp_path)
    marker = "/private/knowledge-canary"
    manifest = tmp_path / "runbooks/catalog.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["documents"][0]["source_path"] = marker + ".md"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert index_corpus(tmp_path).outcome == "failed"
    status = client.get("/api/knowledge/index-status", headers=headers)
    assert status.status_code == 200
    assert status.json()["last_attempt"]["outcome"] == "failed"
    assert marker not in status.text


def test_rejected_key_version_are_not_echoed_in_public_status(client, tmp_path):
    marker = "private-corpus-key-canary"
    write_corpus(
        tmp_path,
        [document(document_key=marker, source_path=f"runbooks/{marker}.md")],
        {marker: "# Private candidate\n\nRetained original.\n"},
    )
    assert index_corpus(tmp_path).outcome == "indexed"
    write_corpus(
        tmp_path,
        [document(document_key=marker, source_path=f"runbooks/{marker}.md")],
        {marker: "# Private candidate\n\nConflicting rejected bytes.\n"},
    )
    failed = index_corpus(tmp_path)
    assert failed.outcome == "failed"
    assert marker not in failed.reason

    status = client.get("/api/knowledge/index-status", headers=_headers(client))
    assert status.status_code == 200
    assert marker not in status.text
    assert status.json()["last_attempt"]["outcome"] == "failed"
