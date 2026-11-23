import hashlib
import json

import pytest

from cloudops.features.evaluation.bundle import EvalInputError, from_context, load, verify
from cloudops.features.evaluation.hashing import content_sha256
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
