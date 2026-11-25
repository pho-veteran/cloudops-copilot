import hashlib
import json

import pytest

from cloudops.features.evaluation.bundle import EvalInputError, from_context, load, verify
from cloudops.features.evaluation.hashing import content_sha256
from cloudops.features.knowledge.chunking import chunk_document
from eval_fixtures import context_for_case, write_bundle


def test_bundle_preserves_the_exact_prepared_context(tmp_path):
    context = context_for_case("crashloop-positive")
    bundle = from_context(
        context,
        case_id="crashloop-positive",
        bundle_id="controlled-crashloop",
        captured_at=context.window.start,
    )
    path = write_bundle(tmp_path / "bundle.json", bundle)
    loaded, raw_sha = load(path, expected_case_id="crashloop-positive")
    assert loaded.context == context
    assert raw_sha == hashlib.sha256(path.read_bytes()).hexdigest()


def test_bundle_rejects_changed_runbook_full_text():
    context = context_for_case("crashloop-positive")
    bundle = from_context(
        context,
        case_id="crashloop-positive",
        bundle_id="controlled-crashloop",
        captured_at=context.window.start,
    )
    changed_runbook = bundle.context.runbooks[0].model_copy(
        update={"content": bundle.context.runbooks[0].content + " altered"}
    )
    changed_context = bundle.context.model_copy(update={"runbooks": [changed_runbook]})
    tampered = bundle.model_copy(
        update={"context": changed_context, "manifest_sha256": content_sha256(changed_context)}
    )
    with pytest.raises(EvalInputError, match="runbook_digest_mismatch"):
        verify(tampered)


def test_bundle_accepts_a_production_chunk_of_a_multi_section_runbook():
    context = context_for_case("crashloop-positive")
    runbook = context.runbooks[0]
    chunks = chunk_document(runbook.content.encode("utf-8"))
    assert len(chunks) > 1
    chunk = chunks[1]
    section = chunk.content.decode("utf-8")
    reference = runbook.reference.model_copy(update={"chunk_sha256": chunk.content_sha256})
    assert reference.document_sha256 != reference.chunk_sha256
    chunked = runbook.model_copy(
        update={
            "reference": reference,
            "heading_path": list(chunk.heading_path),
            "content": section,
            "content_bytes": len(chunk.content),
        }
    )
    changed = context.model_copy(update={"runbooks": [chunked]})
    bundle = from_context(
        changed, case_id="crashloop-positive", bundle_id="chunked", captured_at=context.window.start
    )
    assert bundle.context.runbooks[0].reference.document_sha256 == runbook.reference.document_sha256


def test_bundle_rejects_label_shaped_extra_fields(tmp_path):
    context = context_for_case("crashloop-positive")
    bundle = from_context(
        context,
        case_id="crashloop-positive",
        bundle_id="controlled-crashloop",
        captured_at=context.window.start,
    )
    payload = json.loads(bundle.model_dump_json())
    payload["label"] = {"category": "oom_restarts", "rationale": "held out"}
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvalInputError, match="bundle_schema"):
        load(path, expected_case_id="crashloop-positive")
