"""Stage A signals and stage B detector on the E06 fixtures, with no model call."""

import json
from pathlib import Path

import pytest
from eval_fixtures import _material

from cloudops.features.diagnosis.context import family_matrix
from cloudops.features.diagnosis.engine import detector, signals
from cloudops.features.diagnosis.engine.signals import SIGNALS_VERSION
from cloudops.features.diagnosis.versions import SOURCE_CLASSES
from eval_fixtures import _policy

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "services/backend/tests/fixtures/e06"
CASES = {item["id"]: item for item in json.loads((FIXTURES / "cases.json").read_text())}
ORACLE = json.loads((FIXTURES / "oracle.json").read_text())["cases"]


def _materials(case_id: str):
    return [
        _material(slot, payload, case_id)
        for slot, payload in CASES[case_id]["evidence"].items()
        if slot in SOURCE_CLASSES
    ]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_detector_matches_the_fixture_oracle(case_id):
    materials = _materials(case_id)
    result = detector.detect(signals.extract(materials), family_matrix(materials, _policy()))
    oracle = ORACLE[case_id]
    expected = oracle["category"] if oracle["required_result"] == "supported" else "unknown"
    assert result.family == expected
    if expected != "unknown":
        top = result.hypotheses[0]
        assert top.support, "a detected family keeps its supporting entries"
        keys = {
            (str(item.evidence_id), entry.kind, entry.key)
            for item in materials
            for entry in item.entries
        }
        assert {(ref.evidence_id, ref.kind, ref.key) for ref in top.support} <= keys


def test_injection_text_does_not_change_the_detection():
    clean = _materials("c01")
    hostile = _materials("n-injection")
    left = detector.detect(signals.extract(clean), family_matrix(clean, _policy()))
    right = detector.detect(signals.extract(hostile), family_matrix(hostile, _policy()))
    assert left.family == right.family == "crashloop_startup"
    assert [item.family for item in left.hypotheses] == [item.family for item in right.hypotheses]


def test_ineligible_top_family_abstains():
    materials = [item for item in _materials("c01") if item.slot != "kubernetes.logs.previous"]
    result = detector.detect(signals.extract(materials), family_matrix(materials, _policy()))
    assert result.family == "unknown" and result.reason == "top_family_ineligible"


def test_versions_are_pinned():
    assert SIGNALS_VERSION == "engine.signals/v1"
    assert detector.DETECTOR_VERSION == "engine.detector/v1"
