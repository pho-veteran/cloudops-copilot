from pathlib import Path

import pytest

from cloudops.features.evaluation.seal import (
    EvalSealError,
    _PINNED_SOURCES,
    REPO_ROOT,
    freeze,
    verify,
)
from eval_fixtures import write_corpus

SOURCE_REVISION = "0f6afb146e515d3296ffb2b31348226d3c5f037c"


def _freeze(tmp_path: Path) -> Path:
    fixture_root = tmp_path / "fixtures"
    write_corpus(fixture_root)
    state = tmp_path / "state"
    freeze(
        state,
        fixture_root / "cohort.json",
        fixture_root / "labels.json",
        fixture_root / "rubric.json",
        SOURCE_REVISION,
    )
    return state


def test_freeze_copies_inputs_bundles_and_pinned_sources(tmp_path):
    state = _freeze(tmp_path)
    seal = verify(state)
    assert seal.source_revision == SOURCE_REVISION
    assert (state / "bundles/crashloop-positive.json").is_file()
    assert (state / "source/services/backend/src/cloudops/features/evaluation/rules.py").is_file()


def test_seal_rejects_changed_frozen_labels(tmp_path):
    state = _freeze(tmp_path)
    labels_path = state / "inputs/labels.json"
    labels_path.write_text(labels_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(EvalSealError, match="seal_mismatch"):
        verify(state)


def test_seal_rejects_drift_in_live_scoring_source(tmp_path, monkeypatch):
    state = _freeze(tmp_path)
    live_root = tmp_path / "live-source"
    for relative in _PINNED_SOURCES:
        source = REPO_ROOT / relative
        target = live_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    drifted = live_root / "services/backend/src/cloudops/features/evaluation/versions.py"
    drifted.write_bytes(drifted.read_bytes() + b"\n# drift\n")
    monkeypatch.setattr("cloudops.features.evaluation.seal.REPO_ROOT", live_root)
    with pytest.raises(EvalSealError, match="source_drift"):
        verify(state)
