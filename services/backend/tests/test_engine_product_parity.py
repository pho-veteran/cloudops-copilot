"""Trusted parity check: the product engine profile against the offline B4 replay.

Runs only when ``ENGINE_PARITY_DIR`` names a directory of cases prepared from engine-loop
state (``<case>.json`` with the raw bundle, the B4 replay record and the cached model
text). Each bundle is stored as a persisted Run with its original identities, the
product builds the context with ``context_profile="engine"`` and validates the cached
answer with the profile's stage E. Context digest and decision must equal the replay.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from cloudops.features.access.models import User
from cloudops.features.diagnosis.advice import validate
from cloudops.features.diagnosis.context import PreparedContext, build
from cloudops.features.diagnosis.engine import path
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.workloads.models import Workload

PARITY = os.environ.get("ENGINE_PARITY_DIR")
pytestmark = pytest.mark.skipif(not PARITY, reason="trusted parity input not supplied")


def _policy() -> EgressPolicy:
    # The engine-loop replay policy (scripts/engine_loop/pipeline.py `policy`).
    return EgressPolicy.model_validate(json.loads((Path(PARITY) / "policy.json").read_text()))


def _store(db, actor, bundle: dict) -> tuple:
    scope = bundle["scope"]
    db.add(
        Workload(
            id=scope["workload_id"],
            cluster=scope["cluster"],
            namespace=scope["namespace"],
            name=scope["deployment"],
            uid=scope["uid"],
        )
    )
    db.flush()
    db.add(
        Incident(
            id=scope["incident_id"],
            workload_id=scope["workload_id"],
            title="Parity",
            description="Parity",
            created_by=actor.id,
        )
    )
    db.flush()
    db.add(
        InvestigationRun(
            id=scope["run_id"],
            incident_id=scope["incident_id"],
            state="insufficient_evidence",
            requested_by=actor.id,
            question=scope["request_text"],
            question_source=scope["request_source"],
            window_start=scope["window"]["start"],
            window_end=scope["window"]["end"],
            window_source=scope["window"]["source"],
            pack_coverage=[],
        )
    )
    db.flush()
    for item in bundle["slots"]:
        if item["evidence_id"] is None or item["slot"] not in SOURCES:
            continue
        spec = SOURCES[item["slot"]]
        db.add(
            Evidence(
                id=item["evidence_id"],
                run_id=scope["run_id"],
                workload_uid=scope["uid"],
                source_key=item["slot"],
                source_kind=spec.source_kind,
                catalog_version=CATALOG_VERSION,
                schema_version=2,
                payload_kind=item["payload_kind"] or spec.payload_kind,
                payload_version=item["payload_version"] or 1,
                collected_at=bundle["captured_at"],
                time_relation=item["time_relation"],
                coverage={
                    "status": item["coverage_status"],
                    "reason_codes": item["coverage_reason_codes"],
                },
                redaction_policy_version="redaction/v1",
                sha256=item["sha256"],
                payload=item["payload"],
            )
        )
    db.commit()
    return scope["incident_id"], scope["run_id"]


def _cases():
    return sorted(Path(PARITY).glob("case-*.json")) if PARITY else []


@pytest.mark.parametrize("case_file", _cases(), ids=lambda item: item.stem)
def test_product_engine_profile_matches_replay(db, seeded, case_file):
    case = json.loads(case_file.read_text())
    actor = db.scalar(select(User).where(User.username == "operator"))
    incident_id, run_id = _store(db, actor, case["bundle"])
    model = ModelProfile.model_validate(case["profile"])
    built = build(
        db,
        incident_id,
        run_id,
        selection=None,
        policy=_policy(),
        profile=model,
        analysis_identity=f"engine-loop.{case['bundle']['case_id']}",
        context_profile="engine",
    )
    replay = case["record"]
    assert built.digest == replay["context"]["digest"]
    if not isinstance(built, PreparedContext):
        assert replay["final"]["status"] == "insufficient_evidence"
        return
    outcome = validate(db, built, case["text"], refine=path.refine)
    assert outcome.status == "valid"
    final = replay["final"]
    assert outcome.result.status == final["status"]
    assert outcome.result.cause_category == final["cause_category"]
    supporting = [item.alias for item in outcome.result.supporting]
    assert supporting == final["supporting_evidence_ids"]
